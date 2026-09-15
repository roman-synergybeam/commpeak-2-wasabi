"""Deleting verified recordings from CommPeak, one at a time, or not at all.

This is the irreversible end of the system. Everything in it is arranged so
that the default outcome is "nothing happened" and a delete requires every
condition to be affirmatively true.

The check that does the real work is `_still_in_the_archive`. Every other
condition is about policy and can be reasoned about in advance; this one is
about the world right now. `verified_at` records that the copy was good when
it was made -- a lifecycle rule, a bucket policy change or somebody tidying up
could have removed it since, and nothing would have told us. So the
destination object is fetched again, its size compared and its stored
`c2w-sha256` compared, immediately before the source copy is removed. If that
HEAD fails for any reason at all, including a network blip, the source object
is kept. The cost of a false negative is a recording deleted later; the cost of
a false positive is a recording that no longer exists anywhere.

`dry_run` is the default. It runs the identical path, including the archive
re-check, and stops before the delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.storage.errors import TransferError, classify_exception

__all__ = ["DeleteRun", "run_source_deletion"]

log = get_logger(__name__)

#: Classes that mean the source is refusing us. One is enough to stop the pass.
_REFUSALS = ("ACL_ERROR", "AUTH_ERROR", "RATE_LIMIT", "NETWORK_ERROR")


@dataclass(slots=True)
class DeleteRun:
    account: str
    dry_run: bool
    considered: int = 0
    deleted: int = 0
    kept: int = 0
    bytes_freed: int = 0
    stopped_early: str = ""
    kept_reasons: dict[str, int] = field(default_factory=dict)

    def keep(self, reason: str) -> None:
        self.kept += 1
        self.kept_reasons[reason] = self.kept_reasons.get(reason, 0) + 1


async def _still_in_the_archive(destination: Any, recording: Any) -> tuple[bool, str]:
    """Is the archived copy there, right now, and the same object?

    Returns (ok, why-not). Anything other than a clean match is a no: this is
    the last check before something becomes unrecoverable, so an error and a
    mismatch are treated the same.
    """
    try:
        head = await destination.head(recording["destination_key"])
    except Exception as exc:
        error = exc if isinstance(exc, TransferError) else classify_exception(exc)
        return False, f"archive did not answer ({getattr(error, 'error_class', 'UNKNOWN')})"
    if head is None:
        return False, "archive has no object at the recorded key"

    size = getattr(head, "size", None)
    if size is not None and recording["destination_size"] is not None:
        if int(size) != int(recording["destination_size"]):
            return False, "archived object is a different size than recorded"

    stored = (getattr(head, "metadata", None) or {}).get("c2w-sha256")
    if stored and recording["checksum_sha256"] and stored != recording["checksum_sha256"]:
        return False, "archived object's checksum does not match the one recorded"
    if not stored:
        # Not fatal on its own -- older objects predate the metadata -- but the
        # size check above then carries the whole weight, so it is recorded.
        return True, "no checksum stored on the archived object; size matched"
    return True, ""


async def run_source_deletion(
    session: AsyncSession,
    *,
    connection_id: int,
    account: str,
    brand_id: int,
    candidates: list[dict[str, Any]],
    destination: Any,
    deleter: Any | None,
    dry_run: bool = True,
) -> DeleteRun:
    """Delete the candidates whose archived copy is verified again, now.

    `deleter` may be None only for a dry run. A real run without one is a
    programming error rather than a no-op, because a caller that believes it is
    deleting and is not would report success for work that never happened.
    """
    if not dry_run and deleter is None:
        raise ValueError("a real deletion run needs a deleter; refusing to pretend")

    run = DeleteRun(account=account, dry_run=dry_run)
    for recording in candidates:
        run.considered += 1

        ok, why = await _still_in_the_archive(destination, recording)
        if not ok:
            run.keep(why)
            log.warning(
                "source_delete.kept",
                recording_id=recording["id"],
                account=account,
                reason=why,
            )
            continue
        if why:
            log.info("source_delete.note", recording_id=recording["id"], note=why)

        if dry_run or deleter is None:
            # `deleter is None` cannot happen on a real run -- the guard at the
            # top refuses that -- but narrowing it here keeps the delete call
            # unreachable unless there is something to delete with, which is
            # the property worth making structural rather than assumed.
            run.deleted += 1        # would have been
            run.bytes_freed += int(recording["source_size"] or 0)
            continue

        outcome = await deleter.delete(recording["source_key"])
        if not outcome.deleted:
            run.keep("the source refused the delete")
            # A refusal stops the pass. Retrying into one is what has taken
            # this source offline three times, and a delete loop is the worst
            # possible place to learn that lesson again.
            if any(cls in outcome.detail for cls in _REFUSALS):
                run.stopped_early = f"source refused: {outcome.detail[:200]}"
                break
            continue

        await session.execute(
            text(
                """
                UPDATE recordings
                   SET state = 'SOURCE_DELETED',
                       source_deleted_at = now(),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": recording["id"]},
        )
        run.deleted += 1
        run.bytes_freed += int(recording["source_size"] or 0)

    if not dry_run:
        await session.commit()
    log.info(
        "source_delete.finished",
        account=account,
        dry_run=dry_run,
        considered=run.considered,
        deleted=run.deleted,
        kept=run.kept,
        stopped_early=run.stopped_early or None,
    )
    return run
