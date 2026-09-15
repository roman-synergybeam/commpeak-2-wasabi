"""The irreversible end of the system, tested for what it refuses to do.

These are mostly negative tests, deliberately. Deleting the last copy of a
customer's call is the worst outcome this codebase can produce, so what is
worth proving is not that deletion works but that it declines in every
circumstance where it is not certain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from c2w.sync.source_delete import run_source_deletion
from c2w.sync.source_retention import refusal_reasons


@dataclass
class _Head:
    size: int
    metadata: dict[str, str]


class _Archive:
    """A destination that answers HEAD however the test wants."""

    def __init__(self, head: Any = None, raises: Exception | None = None) -> None:
        self._head = head
        self._raises = raises
        self.asked: list[str] = []

    async def head(self, key: str) -> Any:
        self.asked.append(key)
        if self._raises is not None:
            raise self._raises
        return self._head


class _Session:
    """Enough session for this module: it runs one UPDATE per delete and
    commits at the end. Recording the calls is what lets a test assert that a
    kept recording was not also marked SOURCE_DELETED."""

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.statements.append((statement, params))
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Deleter:
    def __init__(self, ok: bool = True, detail: str = "") -> None:
        self.ok = ok
        self.detail = detail
        self.deleted: list[str] = []

    async def delete(self, key: str) -> Any:
        self.deleted.append(key)

        @dataclass
        class _Outcome:
            key: str
            deleted: bool
            detail: str

        return _Outcome(key=key, deleted=self.ok, detail=self.detail)


def _candidate(**kw: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "source_key": "recordings/2024/01/01/call.flac",
        "destination_key": "archive/go4rex.pbx/2024/01/01/call.flac",
        "source_size": 750_000,
        "destination_size": 750_000,
        "checksum_sha256": "abc123",
    }
    base.update(kw)
    return base


class TestNothingIsDeletedWithoutProofItIsArchivedNow:
    """`verified_at` says the copy was good *then*. That is not enough.

    A lifecycle rule, a policy change or a mistaken cleanup could have removed
    the archived object since, and nothing would have told us. So the
    destination is asked again immediately before the source copy goes, and
    anything other than a clean match keeps the source.
    """

    async def test_a_missing_archive_object_keeps_the_source(self):
        archive, deleter, session = _Archive(head=None), _Deleter(), _Session()
        run = await run_source_deletion(
            session, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=deleter,
            dry_run=False,
        )
        assert deleter.deleted == [], "deleted a recording with no archived copy"
        assert run.deleted == 0
        assert run.kept == 1

    async def test_an_unreachable_archive_keeps_the_source(self):
        """A network blip must not read as 'safe to delete'. The cost of
        waiting is a later delete; the cost of guessing is a lost recording."""
        archive = _Archive(raises=OSError("connection reset"))
        deleter, session = _Deleter(), _Session()
        run = await run_source_deletion(
            session, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=deleter,
            dry_run=False,
        )
        assert deleter.deleted == []
        assert run.kept == 1

    async def test_a_size_mismatch_keeps_the_source(self):
        archive = _Archive(head=_Head(size=12, metadata={"c2w-sha256": "abc123"}))
        deleter, session = _Deleter(), _Session()
        run = await run_source_deletion(
            session, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=deleter,
            dry_run=False,
        )
        assert deleter.deleted == []
        assert run.kept == 1

    async def test_a_checksum_mismatch_keeps_the_source(self):
        archive = _Archive(
            head=_Head(size=750_000, metadata={"c2w-sha256": "something-else"})
        )
        deleter, session = _Deleter(), _Session()
        run = await run_source_deletion(
            session, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=deleter,
            dry_run=False,
        )
        assert deleter.deleted == []
        assert run.kept == 1


class TestADryRunDeletesNothing:
    async def test_the_dry_run_never_calls_the_deleter(self):
        archive = _Archive(head=_Head(size=750_000, metadata={"c2w-sha256": "abc123"}))
        deleter = _Deleter()
        run = await run_source_deletion(
            None, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=deleter,
            dry_run=True,
        )
        assert deleter.deleted == []
        assert run.deleted == 1, "a dry run still reports what it would have done"
        assert run.dry_run is True

    async def test_the_dry_run_still_checks_the_archive(self):
        """Otherwise it proves nothing about the run it is standing in for."""
        archive = _Archive(head=_Head(size=750_000, metadata={"c2w-sha256": "abc123"}))
        await run_source_deletion(
            None, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate()], destination=archive, deleter=None,
            dry_run=True,
        )
        assert archive.asked, "the dry run skipped the archive re-check"

    async def test_a_real_run_without_a_deleter_is_refused(self):
        """Not a silent no-op: a caller that believes it is deleting and is
        not would report success for work that never happened."""
        archive = _Archive(head=_Head(size=750_000, metadata={"c2w-sha256": "abc123"}))
        with pytest.raises(ValueError, match="refusing to pretend"):
            await run_source_deletion(
                None, connection_id=1, account="go4rex.pbx", brand_id=1,
                candidates=[_candidate()], destination=archive, deleter=None,
                dry_run=False,
            )


class TestARefusalStopsTheWholePass:
    async def test_a_refused_delete_halts_rather_than_continuing(self):
        """Retrying into a refusal has taken this source offline three times.
        A delete loop is the worst place to repeat it."""
        archive = _Archive(head=_Head(size=750_000, metadata={"c2w-sha256": "abc123"}))
        deleter, session = _Deleter(ok=False, detail="[ACL_ERROR] Forbidden"), _Session()
        run = await run_source_deletion(
            session, connection_id=1, account="go4rex.pbx", brand_id=1,
            candidates=[_candidate(id=i) for i in range(1, 11)],
            destination=archive, deleter=deleter, dry_run=False,
        )
        assert len(deleter.deleted) == 1, "kept going after a refusal"
        assert run.stopped_early
        assert run.deleted == 0


class TestTheReasonsAreLegible:
    def test_every_missing_precondition_is_named(self):
        reasons = refusal_reasons(
            has_delete_credentials=False,
            account_enabled=False,
            completeness_blocking=["600,151 recordings are not verified"],
            global_enabled=False,
        )
        # Four, not three: the completeness blocker is a reason in its own
        # right and is passed through rather than summarised away.
        assert len(reasons) == 4, reasons
        assert any("not verified" in r for r in reasons)
        assert any("whole platform" in r for r in reasons)
        assert any("not switched on for this account" in r for r in reasons)
        assert any("delete credentials" in r for r in reasons)

    def test_nothing_to_say_when_everything_is_in_place(self):
        assert refusal_reasons(
            has_delete_credentials=True,
            account_enabled=True,
            completeness_blocking=[],
            global_enabled=True,
        ) == []


class TestTheDeleterCannotExistWithoutCredentials:
    def test_building_one_without_credentials_raises(self):
        """The structural guarantee: with no delete credentials there is no
        object capable of deleting, whatever any setting says."""
        from c2w.storage.base import S3Credentials
        from c2w.storage.deleter import CommPeakDeleter

        with pytest.raises(ValueError, match="delete credentials"):
            CommPeakDeleter(
                S3Credentials(
                    endpoint_url="https://recordings.commpeak.com",
                    access_key="", secret_key="", bucket="b",
                )
            )

    def test_the_read_path_still_cannot_delete(self):
        """`CommPeakSource` must stay incapable, not merely discouraged."""
        import inspect

        from c2w.storage.commpeak import CommPeakSource

        source = inspect.getsource(CommPeakSource)
        assert "SourceIsReadOnly" in source
        assert "delete_object" not in source
