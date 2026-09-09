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

from cprec.db.base import ConnectionStatus, JobKind, RecordingState, SyncRunKind
from cprec.db.models.core import CommPeakConnection, Recording, SyncRun
from cprec.db.session import dispose_engine, get_platform_engine, platform_session
from cprec.logging import configure_logging, get_logger, reconfigure_from_settings
from cprec.settings import settings_service
from cprec.storage.errors import TransferError, classify_exception
from cprec.storage.factory import open_source
from cprec.sync import queue
from cprec.sync.inventory import backfill_priority, plan_incremental, scan_range

log = get_logger(__name__)

TICK_SECONDS = 30.0


class Scheduler:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        self._last_inventory = 0.0
        self._last_retention = 0.0

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
    configure_logging("cprec-scheduler")
    async with platform_session() as session:
        await reconfigure_from_settings("cprec-scheduler", session)

    async with singleton_lock("cprec-scheduler") as acquired:
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
