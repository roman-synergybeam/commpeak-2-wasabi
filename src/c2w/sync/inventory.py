"""Inventory: discover recordings in a CommPeak bucket and record them.

Scanning is done one hour-prefix at a time.  That is the single most important
decision in this module: the largest bucket in play holds 12.9M objects, and a
flat listing of it is a multi-hour operation that must survive a restart.  An
hour prefix is a cheap, independent listing whose completion can be recorded,
so a scan interrupted after eight hours of work resumes where it stopped
instead of starting again.

Discovery is decoupled from transfer on purpose.  Inventory and correlation run
whether or not an archive destination exists yet -- which is the situation while
Wasabi buckets are still being provisioned -- so the CDR UI becomes useful
immediately and transfers begin later without a re-scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.commpeak.correlate import CdrCandidate, MatchMethod, correlate
from c2w.commpeak.keyparse import ParsedKey, iter_hour_prefixes, parse_key
from c2w.db.base import JobKind, RecordingState
from c2w.db.models.core import CommPeakConnection, Recording
from c2w.logging import get_logger
from c2w.storage.commpeak import CommPeakSource
from c2w.sync import queue

log = get_logger(__name__)

__all__ = ["InventoryResult", "backfill_priority", "plan_incremental", "scan_hour", "scan_range"]

#: Newest recordings matter most: they are what people ask to hear, so the
#: current month should drain before years of history. Priority is derived from
#: age so a single ordering rule covers both backfill and live traffic.
_PRIORITY_RECENT = 10
_PRIORITY_MONTH = 50
_PRIORITY_QUARTER = 200
_PRIORITY_HISTORIC = 500


def backfill_priority(started_at: datetime | None, *, now: datetime | None = None) -> int:
    """Map a recording's age onto a queue priority (lower runs first)."""
    if started_at is None:
        return _PRIORITY_HISTORIC
    now = now or datetime.now(UTC)
    age = now - started_at
    if age <= timedelta(days=2):
        return _PRIORITY_RECENT
    if age <= timedelta(days=31):
        return _PRIORITY_MONTH
    if age <= timedelta(days=93):
        return _PRIORITY_QUARTER
    return _PRIORITY_HISTORIC


@dataclass(slots=True)
class InventoryResult:
    """Counters from one scan, surfaced on the sync dashboard."""

    prefixes_scanned: int = 0
    objects_seen: int = 0
    recordings_new: int = 0
    recordings_existing: int = 0
    non_audio_skipped: int = 0
    bytes_seen: int = 0
    queued: int = 0
    correlated: int = 0
    orphans: int = 0
    match_methods: dict[str, int] = field(default_factory=dict)
    last_prefix: str | None = None

    def merge(self, other: InventoryResult) -> None:
        self.prefixes_scanned += other.prefixes_scanned
        self.objects_seen += other.objects_seen
        self.recordings_new += other.recordings_new
        self.recordings_existing += other.recordings_existing
        self.non_audio_skipped += other.non_audio_skipped
        self.bytes_seen += other.bytes_seen
        self.queued += other.queued
        self.correlated += other.correlated
        self.orphans += other.orphans
        for k, v in other.match_methods.items():
            self.match_methods[k] = self.match_methods.get(k, 0) + v
        self.last_prefix = other.last_prefix or self.last_prefix


async def _candidates_for(
    session: AsyncSession, connection: CommPeakConnection, parsed: ParsedKey, window_seconds: int
) -> list[CdrCandidate]:
    """Fetch plausible CDRs for one recording.

    Narrowed by connection and a tight time window before ranking, so
    correlation is an indexed lookup over a handful of rows rather than a scan.
    Anchored on the channel-id epoch when available, since that is the most
    accurate timestamp the filename gives us.
    """
    anchor = parsed.uniqueid_time or parsed.started_at or parsed.prefix_hour
    if anchor is None:
        return []
    lo = anchor - timedelta(seconds=window_seconds)
    hi = anchor + timedelta(seconds=window_seconds)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, call_uuid, start_at, src, dst, call_duration, agent_extension
                FROM cdrs
                WHERE brand_id = :brand_id
                  AND connection_id = :connection_id
                  AND start_at BETWEEN :lo AND :hi
                ORDER BY start_at
                LIMIT 50
                """
            ),
            {
                "brand_id": connection.brand_id,
                "connection_id": connection.id,
                "lo": lo,
                "hi": hi,
            },
        )
    ).all()
    return [
        CdrCandidate(
            cdr_id=r.id,
            call_uuid=r.call_uuid,
            start_at=r.start_at,
            src=r.src,
            dst=r.dst,
            call_duration=r.call_duration,
            agent_extension=r.agent_extension,
        )
        for r in rows
    ]


async def scan_hour(
    session: AsyncSession,
    source: CommPeakSource,
    connection: CommPeakConnection,
    hour: datetime,
    *,
    page_size: int = 1000,
    destination_id: int | None = None,
    enqueue_transfers: bool = True,
    correlation_window_seconds: int = 120,
) -> InventoryResult:
    """Inventory one hour prefix.

    Every audio object found is recorded, correlated and (when a destination
    exists) queued.  Objects that fail to correlate are still recorded and still
    queued: losing access to audio because a metadata join failed would be a far
    worse outcome than a call row with thin metadata.
    """
    from c2w.commpeak.keyparse import hour_prefix

    result = InventoryResult(prefixes_scanned=1, last_prefix=hour_prefix(hour))

    async for ref in source.list_prefix(hour_prefix(hour), page_size=page_size):
        result.objects_seen += 1
        parsed = parse_key(ref.key)

        # CommPeak buckets also contain sidecar/JSON artefacts; those are not
        # media and must not become playable "recordings".
        if not parsed.is_audio:
            result.non_audio_skipped += 1
            continue

        existing_id = (
            await session.execute(
                select(Recording.id).where(
                    Recording.brand_id == connection.brand_id,
                    Recording.connection_id == connection.id,
                    Recording.source_key == ref.key,
                )
            )
        ).scalar_one_or_none()

        if existing_id is not None:
            result.recordings_existing += 1
            continue

        candidates = await _candidates_for(session, connection, parsed, correlation_window_seconds)
        match = correlate(parsed, candidates)

        recording = Recording(
            brand_id=connection.brand_id,
            connection_id=connection.id,
            tenant_id=connection.tenant_id,
            source_key=ref.key,
            source_size=ref.size,
            source_etag=ref.etag,
            source_last_modified=ref.last_modified,
            direction=parsed.direction,
            number=parsed.number,
            extension=parsed.extension,
            started_at=parsed.started_at or parsed.uniqueid_time,
            uniqueid=parsed.uniqueid,
            seq=parsed.seq,
            call_group_key=parsed.call_group_key,
            file_ext=parsed.file_ext,
            key_parsed_ok=parsed.parsed_ok,
            cdr_id=match.cdr_id,
            call_uuid=match.call_uuid,
            match_method=str(match.method),
            match_confidence=match.confidence,
            match_ambiguous=match.ambiguous,
            correlated_at=datetime.now(UTC) if match.matched else None,
            state=RecordingState.DISCOVERED,
            destination_id=destination_id,
        )
        session.add(recording)
        await session.flush()

        result.recordings_new += 1
        result.bytes_seen += ref.size
        result.match_methods[str(match.method)] = result.match_methods.get(str(match.method), 0) + 1
        if match.matched:
            result.correlated += 1
        if match.method is MatchMethod.ORPHAN:
            result.orphans += 1

        # Only queue when there is somewhere to put the file. Until a Wasabi
        # destination is configured, recordings accumulate as DISCOVERED and are
        # queued later by the scheduler with no re-scan needed.
        if enqueue_transfers and destination_id is not None:
            await queue.enqueue(
                session,
                brand_id=connection.brand_id,
                connection_id=connection.id,
                recording_id=recording.id,
                destination_id=destination_id,
                kind=JobKind.TRANSFER,
                priority=backfill_priority(recording.started_at),
            )
            recording.state = RecordingState.QUEUED
            result.queued += 1

    return result


async def scan_range(
    session: AsyncSession,
    source: CommPeakSource,
    connection: CommPeakConnection,
    *,
    start: datetime,
    end: datetime,
    page_size: int = 1000,
    destination_id: int | None = None,
    enqueue_transfers: bool = True,
    commit_every_prefix: bool = True,
) -> InventoryResult:
    """Inventory every hour in ``[start, end]``, oldest first.

    Commits after each prefix by default so progress survives a crash. At
    12.9M objects the alternative -- one enormous transaction -- would both hold
    locks for hours and lose everything on a restart.
    """
    total = InventoryResult()
    for prefix in iter_hour_prefixes(start, end):
        hour = datetime.strptime(prefix.strip("/"), "%Y/%m/%d/%H").replace(tzinfo=UTC)
        got = await scan_hour(
            session,
            source,
            connection,
            hour,
            page_size=page_size,
            destination_id=destination_id,
            enqueue_transfers=enqueue_transfers,
        )
        total.merge(got)

        # Record how far we got, so an incremental pass resumes from here.
        connection.inventory_cursor_hour = hour
        connection.last_inventory_at = datetime.now(UTC)
        if commit_every_prefix:
            await session.commit()

        if got.objects_seen:
            log.info(
                "inventory.hour_scanned",
                connection_id=connection.id,
                prefix=prefix,
                objects=got.objects_seen,
                new=got.recordings_new,
                orphans=got.orphans,
            )
    return total


def plan_incremental(
    connection: CommPeakConnection,
    *,
    overlap_hours: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Decide which hours the next incremental pass should re-list.

    Starts slightly before the last scanned hour, because a recording can be
    written to an hour bucket after we have already passed it -- a call that
    began at 10:59 and ran for ten minutes lands in the 10:00 prefix well after
    that hour ended. Without the overlap those recordings would be missed
    permanently.
    """
    now = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    cursor = connection.inventory_cursor_hour
    if cursor is None:
        # Never scanned: look at today only. A full backfill is an explicit,
        # separately-planned operation, not something an incremental poll
        # should stumble into.
        return now - timedelta(hours=max(overlap_hours, 1)), now
    start = cursor - timedelta(hours=overlap_hours)
    return min(start, now), now
