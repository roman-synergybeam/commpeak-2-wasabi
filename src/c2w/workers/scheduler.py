"""Scheduler: incremental inventory, CDR polling and retention gating.

Runs as a singleton.  Two schedulers would double-queue every scan, and since
each scan can enqueue millions of jobs, that is not a harmless duplicate.

Each activity is independent and failure-isolated: a CommPeak connection whose
ACL has lapsed must not stop the others from being scanned.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from c2w.db.base import ConnectionStatus, JobKind, RecordingState, SyncRunKind
from c2w.db.models.core import CommPeakConnection, Recording, SyncRun
from c2w.db.session import dispose_engine, get_platform_engine, platform_session
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.settings import settings_service
from c2w.storage.errors import TransferError, classify_exception
from c2w.storage.factory import open_source
from c2w.sync import queue
from c2w.sync.inventory import backfill_priority, plan_incremental, scan_range

log = get_logger(__name__)

TICK_SECONDS = 30.0


class Scheduler:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        self._last_inventory = 0.0
        self._last_retention = 0.0
        self._last_sms = 0.0
        self._last_transcribe = 0.0

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        log.info("scheduler.started")
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.exception("scheduler.tick_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
        log.info("scheduler.stopped")

    async def _tick(self) -> None:
        loop_now = asyncio.get_running_loop().time()
        async with platform_session() as session:
            poll = await settings_service.get_int(session, "source.incremental_poll_seconds")

        if loop_now - self._last_inventory >= poll:
            self._last_inventory = loop_now
            await self._run_incremental_inventory()

        # Text messages, for organisations that have switched them on. Its own
        # interval because it is a different API with a different rhythm: a
        # delivery receipt can arrive hours after the message, so this re-reads
        # a wide window rather than only asking for what is new.
        async with platform_session() as session:
            sms_minutes = await settings_service.get_int(session, "sms.poll_minutes")
        if loop_now - self._last_sms >= max(60, sms_minutes * 60):
            self._last_sms = loop_now
            await _poll_text_messages()

        # Transcription, for organisations that have switched it on. Every
        # minute at most: each pass takes only as many recordings as the
        # concurrency setting allows, so a long backlog drains steadily
        # instead of one pass running for hours.
        if loop_now - self._last_transcribe >= 60:
            self._last_transcribe = loop_now
            await _transcribe_pending()

        # Retention only gates which discovered recordings become eligible for
        # offload; it never deletes anything at the source.
        if loop_now - self._last_retention >= 3600:
            self._last_retention = loop_now
            await self._queue_eligible_recordings()

    async def _run_incremental_inventory(self) -> None:
        async with platform_session() as session:
            connections = (
                (
                    await session.execute(
                        select(CommPeakConnection).where(CommPeakConnection.is_enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
        for connection in connections:
            try:
                await self._scan_connection(connection.id)
            except Exception as exc:
                log.warning(
                    "scheduler.inventory_failed",
                    connection_id=connection.id,
                    error=str(exc),
                )

    async def _scan_connection(self, connection_id: int) -> None:
        async with platform_session() as session:
            connection = (
                await session.execute(
                    select(CommPeakConnection).where(CommPeakConnection.id == connection_id)
                )
            ).scalar_one()
            overlap = await settings_service.get_int(session, "source.incremental_overlap_hours")
            page_size = await settings_service.get_int(session, "source.list_page_size")
            transfers_on = await settings_service.get_bool(session, "transfer.enabled")

            start, end = plan_incremental(connection, overlap_hours=overlap)
            run = SyncRun(
                brand_id=connection.brand_id,
                connection_id=connection.id,
                kind=SyncRunKind.INCREMENTAL,
            )
            session.add(run)
            await session.flush()

            try:
                source = await open_source(session, connection)
                async with source as src:
                    result = await scan_range(
                        session,
                        src,
                        connection,
                        start=start,
                        end=end,
                        page_size=page_size,
                        destination_id=connection.destination_id if transfers_on else None,
                        enqueue_transfers=transfers_on,
                        commit_every_prefix=False,
                    )
            except Exception as exc:
                error = exc if isinstance(exc, TransferError) else classify_exception(exc)
                run.finished_at = datetime.now(UTC)
                run.ok = False
                run.error_detail = str(error)[:4000]
                connection.status = ConnectionStatus.ERROR
                connection.status_detail = str(error)[:4000]
                connection.last_probe_at = datetime.now(UTC)
                raise

            run.finished_at = datetime.now(UTC)
            run.ok = True
            run.discovered = result.recordings_new
            run.queued = result.queued
            run.detail = {
                "prefixes": result.prefixes_scanned,
                "objects_seen": result.objects_seen,
                "orphans": result.orphans,
                "match_methods": result.match_methods,
                "bytes_seen": result.bytes_seen,
            }
            connection.status = ConnectionStatus.OK
            connection.status_detail = None
            connection.last_probe_at = datetime.now(UTC)

            if result.recordings_new:
                log.info(
                    "scheduler.inventory_complete",
                    connection_id=connection.id,
                    new=result.recordings_new,
                    queued=result.queued,
                    orphans=result.orphans,
                )

    async def _queue_eligible_recordings(self) -> None:
        """Queue recordings that have aged past their brand's offload window.

        Discovery and offload are separate steps: a recording is inventoried
        immediately so it is searchable, but only copied once it is older than
        the retention window -- there is no point archiving a call that CommPeak
        is still holding anyway. This is also the path that picks up everything
        discovered while no destination existed.
        """
        async with platform_session() as session:
            if not await settings_service.get_bool(session, "transfer.enabled"):
                return

            connections = (
                (
                    await session.execute(
                        select(CommPeakConnection).where(
                            CommPeakConnection.is_enabled.is_(True),
                            CommPeakConnection.destination_id.isnot(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            for connection in connections:
                days = await settings_service.get_int(
                    session, "retention.offload_after_days", brand_id=connection.brand_id
                )
                cutoff = datetime.now(UTC) - timedelta(days=days)
                pending = (
                    (
                        await session.execute(
                            select(Recording)
                            .where(
                                Recording.brand_id == connection.brand_id,
                                Recording.connection_id == connection.id,
                                Recording.state == RecordingState.DISCOVERED,
                                Recording.started_at <= cutoff,
                            )
                            .order_by(Recording.started_at.desc())
                            .limit(5000)
                        )
                    )
                    .scalars()
                    .all()
                )
                for recording in pending:
                    await queue.enqueue(
                        session,
                        brand_id=recording.brand_id,
                        connection_id=connection.id,
                        recording_id=recording.id,
                        destination_id=connection.destination_id,
                        kind=JobKind.TRANSFER,
                        priority=backfill_priority(recording.started_at),
                    )
                    recording.state = RecordingState.QUEUED
                    recording.destination_id = connection.destination_id

                if pending:
                    session.add(
                        SyncRun(
                            brand_id=connection.brand_id,
                            connection_id=connection.id,
                            kind=SyncRunKind.RETENTION,
                            finished_at=datetime.now(UTC),
                            ok=True,
                            queued=len(pending),
                            detail={"offload_after_days": days},
                        )
                    )
                    log.info(
                        "scheduler.queued_eligible",
                        connection_id=connection.id,
                        queued=len(pending),
                        offload_after_days=days,
                    )


async def _transcribe_pending() -> None:
    """Transcribe archived recordings that have no transcript yet.

    Only archived ones: the audio is read from the archive, never from
    CommPeak. Pulling 13.9 TB through here a second time to transcribe it would
    double the transfer this system exists to do once.

    A failure is written to the transcript row rather than retried for ever, so
    one unreadable file cannot block the queue behind it.
    """
    from c2w.transcribe.base import EngineUnavailable
    from c2w.transcribe.service import build_engine, load_settings, transcribe_recording

    async with platform_session() as session:
        brands = (
            await session.execute(text("SELECT id, name FROM brands WHERE is_active"))
        ).all()

    for brand_id, brand_name in brands:
        async with platform_session() as session:
            config = await load_settings(session, brand_id=brand_id)
            if not config.enabled:
                continue
            try:
                engine = build_engine(config)
            except EngineUnavailable as exc:
                # A recogniser nobody built is a configuration problem for a
                # person, so say it once per pass and move on rather than
                # failing every recording individually.
                log.warning(
                    "transcribe.engine_unavailable",
                    brand_id=brand_id,
                    engine=config.engine,
                    detail=str(exc),
                )
                continue

            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
            )
            pending = (
                await session.execute(
                    text(
                        "SELECT r.id FROM recordings r "
                        "LEFT JOIN transcripts t ON t.recording_id = r.id "
                        "  AND t.brand_id = r.brand_id "
                        "WHERE r.state IN ('AVAILABLE', 'SOURCE_DELETED') "
                        "  AND r.destination_key IS NOT NULL "
                        "  AND t.id IS NULL "
                        "ORDER BY r.started_at DESC NULLS LAST "
                        "LIMIT :n"
                    ),
                    {"n": max(1, config.concurrency)},
                )
            ).scalars().all()

        for recording_id in pending:
            async with platform_session() as session:
                await session.execute(
                    text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
                )
                try:
                    result = await transcribe_recording(
                        session,
                        recording_id,
                        brand_id=brand_id,
                        engine=engine,
                        config=config,
                    )
                    await session.commit()
                except EngineUnavailable as exc:
                    await session.rollback()
                    log.warning(
                        "transcribe.engine_unavailable",
                        brand_id=brand_id,
                        detail=str(exc),
                    )
                    break
                except Exception as exc:
                    await session.rollback()
                    log.exception(
                        "transcribe.pass_failed",
                        recording_id=recording_id,
                        error=str(exc)[:200],
                    )
                    continue
            if result is not None:
                log.info(
                    "transcribe.done",
                    brand=brand_name,
                    recording_id=recording_id,
                    seconds=result.duration_seconds,
                    segments=len(result.segments),
                    language=result.language_detected,
                )


async def _poll_text_messages() -> None:
    """Fetch sent and received messages for every organisation that wants them.

    Per organisation, because the API key, the stream and the switch are all
    per-organisation settings -- and because one company's messages must never
    be written under another's brand id.

    The cursor is stored on the organisation's first CommPeak connection rather
    than on the settings row: TextPeak is billed per account and an
    organisation with no PBX connection at all has no messages to fetch, so
    there is always a connection to hang it on when there is anything to do.
    """
    from c2w.commpeak.sms_client import (
        Direction,
        SmsApiConfig,
        SmsQueryFilters,
        fetch_messages,
        normalise_message,
        poll_window,
        store_messages,
    )

    async with platform_session() as session:
        brands = (
            await session.execute(text("SELECT id, name FROM brands WHERE is_active"))
        ).all()

    for brand_id, brand_name in brands:
        async with platform_session() as session:
            if not await settings_service.get_bool(
                session, "sms.enabled", brand_id=brand_id
            ):
                continue
            token = await settings_service.get_secret(
                session, "sms.api_token", brand_id=brand_id
            )
            if not token:
                log.info("sms.skipped", brand_id=brand_id, reason="no api key")
                continue
            config = SmsApiConfig(
                token=token,
                base_url=await settings_service.get_str(
                    session, "sms.api_base", brand_id=brand_id
                ),
                outgoing_path=await settings_service.get_str(
                    session, "sms.api_path", brand_id=brand_id
                ),
                incoming_path=await settings_service.get_str(
                    session, "sms.incoming_path", brand_id=brand_id
                ),
                stream_id=await settings_service.get_str(
                    session, "sms.stream_id", brand_id=brand_id
                ),
                page_size=await settings_service.get_int(
                    session, "sms.page_size", brand_id=brand_id
                ),
            )
            overlap = await settings_service.get_int(
                session, "sms.overlap_hours", brand_id=brand_id
            )
            row = (
                await session.execute(
                    text(
                        "SELECT id, last_sms_cursor FROM commpeak_connections "
                        "WHERE brand_id = :b ORDER BY id LIMIT 1"
                    ),
                    {"b": brand_id},
                )
            ).first()

        if row is None:
            log.info("sms.skipped", brand_id=brand_id, reason="no commpeak account")
            continue
        connection_id, cursor = row
        start, end = poll_window(cursor, overlap_hours=overlap)
        filters = SmsQueryFilters(start=start, end=end)

        rows: list[dict] = []
        try:
            for direction in (Direction.OUT, Direction.IN):
                items = await fetch_messages(config, direction, filters)
                rows.extend(normalise_message(item, direction) for item in items)
        except Exception as exc:
            # A refused key or an unreachable host must not stop the other
            # organisations, and must not move the cursor -- the window has to
            # be retried, or those messages are lost for good.
            log.warning(
                "sms.poll_failed", brand_id=brand_id, brand=brand_name, error=str(exc)[:200]
            )
            continue

        async with platform_session() as session:
            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
            )
            inserted, updated = await store_messages(
                session, brand_id, rows, connection_id=connection_id
            )
            # Only advanced once the rows are safely stored, so a crash between
            # the fetch and the write re-reads the same window instead of
            # skipping it.
            await session.execute(
                text(
                    "UPDATE commpeak_connections SET last_sms_cursor = :c, "
                    "last_sms_sync_at = now() WHERE id = :i"
                ),
                {"c": end, "i": connection_id},
            )
            await session.commit()
        log.info(
            "sms.polled",
            brand_id=brand_id,
            brand=brand_name,
            fetched=len(rows),
            inserted=inserted,
            updated=updated,
        )


@contextlib.asynccontextmanager
async def singleton_lock(name: str):
    """Hold a PostgreSQL advisory lock for as long as the process runs.

    A second scheduler would double-queue every scan, and a scan can enqueue
    millions of jobs, so this is not a harmless duplicate.

    The lock must be taken on a connection that stays open: advisory locks are
    session-scoped, so acquiring one inside a short-lived session releases it
    the moment that session returns to the pool -- which looks like it worked
    and guards nothing. Holding the connection also means the lock is released
    automatically if the process dies, with no stale-lock cleanup to do.
    """
    engine = get_platform_engine()
    connection = await engine.connect()
    try:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": name}
            )
        ).scalar_one()
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name}
            )
    finally:
        await connection.close()


async def _amain() -> None:
    configure_logging("c2w-scheduler")
    async with platform_session() as session:
        await reconfigure_from_settings("c2w-scheduler", session)

    async with singleton_lock("c2w-scheduler") as acquired:
        if not acquired:
            log.error(
                "scheduler.already_running",
                detail="another scheduler holds the lock; this instance is exiting",
            )
            return

        scheduler = Scheduler()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, scheduler.request_stop)
        try:
            await scheduler.run()
        finally:
            await dispose_engine()


def main() -> int:
    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
