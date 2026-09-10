"""CommPeak recordings bucket as an :class:`~c2w.storage.base.ObjectSource`.

CommPeak's documented S3 access
(https://docs.commpeak.com/docs/recordings-access-accounts-out) requires:

* endpoint ``https://recordings.commpeak.com``
* **path-style** addressing
* the S3 account token/secret as access key / secret key
* the calling server's public IP present in the account's Access Control List
* concurrency of about 5 per account

The bucket name is the account UUID.  Objects live under
``/{year}/{month}/{day}/{hour}/`` and are normally FLAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from c2w.commpeak.keyparse import DEFAULT_KEY_ROOT, day_prefix
from c2w.storage.base import S3Credentials
from c2w.storage.errors import ErrorClass, TransferError
from c2w.storage.s3_adapter import RateLimiter, S3Client

__all__ = ["COMMPEAK_ENDPOINT", "CommPeakSource", "ProbeResult", "commpeak_credentials"]

COMMPEAK_ENDPOINT = "https://recordings.commpeak.com"
#: CommPeak's own recommendation for concurrent transfers per S3 account.
RECOMMENDED_CONCURRENCY = 5


def commpeak_credentials(
    *,
    bucket: str,
    access_key: str,
    secret_key: str,
    endpoint_url: str = COMMPEAK_ENDPOINT,
    region: str = "us-east-1",
) -> S3Credentials:
    """Build credentials with the settings CommPeak requires."""
    return S3Credentials(
        endpoint_url=endpoint_url,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        bucket=bucket,
        path_style=True,
        signature_version="s3v4",
    )


class SourceIsReadOnly(RuntimeError):
    """Raised on any attempt to mutate the CommPeak side.

    The platform copies recordings out of CommPeak and never writes back.  This
    is enforced here rather than left to discipline, because the alternative
    failure -- a stray delete against a bucket holding 12.9M irreplaceable call
    recordings -- is unrecoverable.  CommPeak's own documentation states that
    deleted recordings cannot be restored.
    """


class CommPeakSource(S3Client):
    """Read-only view of one CommPeak recordings bucket.

    Every mutating operation inherited from :class:`S3Client` is overridden to
    raise.  There is no flag to turn this off: re-enabling writes would be a
    deliberate code change with a review attached, not a setting someone can
    flip at 2am.
    """

    def __init__(self, credentials: S3Credentials, *, limiter: RateLimiter | None = None) -> None:
        super().__init__(
            credentials, limiter=limiter, max_pool_connections=RECOMMENDED_CONCURRENCY * 2
        )

    async def delete(self, key: str) -> None:
        raise SourceIsReadOnly(
            f"refusing to delete {key!r} from CommPeak bucket {self.creds.bucket!r}: "
            "the source is read-only and CommPeak deletions are irreversible"
        )

    async def put_stream(self, *args: object, **kwargs: object) -> None:
        raise SourceIsReadOnly("refusing to write to CommPeak: the source is read-only")

    async def put_bytes(self, *args: object, **kwargs: object) -> None:
        raise SourceIsReadOnly("refusing to write to CommPeak: the source is read-only")

    async def list_day(
        self,
        moment: datetime,
        *,
        start_after: str | None = None,
        root: str = DEFAULT_KEY_ROOT,
    ):
        """List one day's folder.  The unit of work for inventory scanning."""
        async for ref in self.list_prefix(day_prefix(moment, root), start_after=start_after):
            yield ref

    async def discover_years(self, root: str = DEFAULT_KEY_ROOT) -> list[int]:
        """Which years this bucket actually contains.

        Cheaper and far more reliable than assuming a start date: a delimiter
        listing returns only the top-level year folders, so a multi-million
        object backfill can be bounded without walking it first.

        Listed under ``root``, not at the bucket root -- the dates sit inside
        `recordings/` on every live account. A non-numeric sibling is skipped
        rather than tripped over: `go4rex.pbx` really does have
        `recordings/default/997/tmp/` alongside its years.
        """
        years: list[int] = []
        for prefix in await self.list_common_prefixes(root):
            token = prefix.rstrip("/").rsplit("/", 1)[-1]
            if token.isdigit() and len(token) == 4:
                years.append(int(token))
        return sorted(years)

    async def earliest_day(self, root: str = DEFAULT_KEY_ROOT) -> datetime | None:
        """Walk year/month/day folders down to the oldest populated day.

        Three levels, not four. There is no hour folder; this walked into a
        fourth level that does not exist, found nothing there, and returned
        ``None`` for every bucket -- so "how far back does this account go"
        was unanswerable on every account that had data.
        """
        parts: list[str] = []
        for _ in range(3):
            children = await self.list_common_prefixes(root + "".join(f"{p}/" for p in parts))
            leaves = (c.rstrip("/").rsplit("/", 1)[-1] for c in children)
            tokens = sorted(t for t in leaves if t.isdigit())
            if not tokens:
                break
            parts.append(tokens[0])
        if len(parts) < 3:
            return None
        year, month, day = (int(p) for p in parts[:3])
        return datetime(year, month, day, tzinfo=UTC)


@dataclass(slots=True)
class ProbeResult:
    """One check from the connection self-test wizard."""

    name: str
    ok: bool
    detail: str = ""
    error_class: ErrorClass | None = None
    hint: str | None = None


async def run_source_probes(
    source: CommPeakSource, *, sample_download: bool = True
) -> list[ProbeResult]:
    """Run the onboarding self-test against a CommPeak connection.

    Each check is reported independently so the operator sees *which* step
    failed -- distinguishing "wrong secret" from "IP not whitelisted" from
    "bucket empty" is most of the work of onboarding a new account.
    """
    results: list[ProbeResult] = []

    def record(name: str, exc: BaseException | None, detail: str = "") -> bool:
        if exc is None:
            results.append(ProbeResult(name, True, detail))
            return True
        err = exc if isinstance(exc, TransferError) else TransferError(ErrorClass.UNKNOWN, str(exc))
        results.append(
            ProbeResult(name, False, err.message, error_class=err.error_class, hint=err.hint)
        )
        return False

    # 1. credentials + ACL + bucket in one call
    try:
        await source.probe()
        ok = record("s3_authentication", None, f"bucket {source.creds.bucket} reachable")
    except BaseException as exc:
        ok = record("s3_authentication", exc)
    if not ok:
        return results

    # 2. which years exist
    try:
        years = await source.discover_years()
        record(
            "bucket_discovery",
            None,
            f"years present: {', '.join(map(str, years)) if years else 'none'}",
        )
    except BaseException as exc:
        years = []
        record("bucket_discovery", exc)

    # 3. can we enumerate a populated day
    sample_key: str | None = None
    sample_size = 0
    try:
        earliest = await source.earliest_day()
        if earliest is None:
            record("recordings_directory", None, "bucket contains no dated folders yet")
        else:
            record("recordings_directory", None, f"oldest recording day {earliest:%Y-%m-%d}")
    except BaseException as exc:
        record("recordings_directory", exc)

    # 4. list operation returns real objects
    try:
        count = 0
        async for ref in source.list_prefix("", page_size=5):
            sample_key = sample_key or ref.key
            sample_size = sample_size or ref.size
            count += 1
            if count >= 5:
                break
        record("list_operation", None, f"listed {count} object(s)")
    except BaseException as exc:
        record("list_operation", exc)

    # 5. HEAD a real object
    if sample_key:
        try:
            meta = await source.head(sample_key)
            record("head_object", None, f"{meta.key} ({meta.size} bytes)")
        except BaseException as exc:
            record("head_object", exc)

        # 6. download the first bytes -- proves GET permission, not just LIST
        if sample_download:
            try:
                got = 0
                async for chunk in source.open_stream(sample_key):
                    got += len(chunk)
                    if got >= 64 * 1024:
                        break
                record("download_test", None, f"read {got} bytes of {sample_key}")
            except BaseException as exc:
                record("download_test", exc)
    else:
        record(
            "head_object",
            None,
            "skipped: no objects available to sample",
        )
    return results
