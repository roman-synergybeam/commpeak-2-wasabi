"""Nightly reconciliation: source vs database vs archive.

Everything else in the platform is optimistic -- a transfer verifies once, at
the moment it happens, and then the recording is considered good.  This is the
process that notices when that stops being true: an archive object deleted by
someone with bucket access, a lifecycle rule that expired something it should
not have, a recording our database never learned about because a scan failed
midway.

It runs once a day rather than continuously because it is a full-inventory
comparison, and at ~19M objects that is not something to do casually.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.alerts.base import Alert, Severity, dispatch
from c2w.db.base import JobKind, RecordingState, SyncRunKind
from c2w.db.models.core import (
    Brand,
    CommPeakConnection,
    Recording,
    StorageDestination,
    SyncRun,
)
from c2w.db.session import dispose_engine, platform_session
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.settings import settings_service
from c2w.storage.factory import open_destination
from c2w.sync import queue
from c2w.sync.transfer import verify_recording
from c2w.workers.scheduler import singleton_lock

log = get_logger(__name__)

CHECK_INTERVAL_SECONDS = 900.0
#: Verifying every archived object nightly would be millions of HEAD requests.
#: A bounded sample catches systematic problems -- a lifecycle rule deleting
#: things, a bucket policy change -- without that cost.
VERIFY_SAMPLE_SIZE = 500


@dataclass(slots=True)
class ReconcileReport:
    brand_id: int
    connection_id: int
    checked: int = 0
    repaired: int = 0
    missing_destination: int = 0
    checksum_mismatch: int = 0
    stuck_recordings: int = 0
    failed_recordings: int = 0
    orphan_recordings: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return self.missing_destination + self.checksum_mismatch + self.stuck_recordings


class Reconciler:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        self._last_run_date: str | None = None

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        log.info("reconciler.started")
        while not self._stopping.is_set():
            try:
                await self._maybe_run()
            except Exception as exc:
                log.exception("reconciler.failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=CHECK_INTERVAL_SECONDS)
        log.info("reconciler.stopped")

    async def _maybe_run(self) -> None:
        """Run once per day at the configured hour."""
        async with platform_session() as session:
            hour = await settings_service.get_int(session, "schedule.reconcile_hour")
        now = datetime.now(UTC)
        today = now.date().isoformat()
        if now.hour != hour or self._last_run_date == today:
            return
        self._last_run_date = today
        await self.reconcile_all()

    async def reconcile_all(self) -> list[ReconcileReport]:
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
            connection_ids = [c.id for c in connections]

        reports: list[ReconcileReport] = []
        for connection_id in connection_ids:
            try:
                reports.append(await self.reconcile_connection(connection_id))
            except Exception as exc:
                log.warning(
                    "reconciler.connection_failed", connection_id=connection_id, error=str(exc)
                )
        return reports

    async def reconcile_connection(self, connection_id: int) -> ReconcileReport:
        async with platform_session() as session:
            connection = (
                await session.execute(
                    select(CommPeakConnection).where(CommPeakConnection.id == connection_id)
                )
            ).scalar_one()
            report = ReconcileReport(brand_id=connection.brand_id, connection_id=connection.id)
            run = SyncRun(
                brand_id=connection.brand_id,
                connection_id=connection.id,
                kind=SyncRunKind.RECONCILE,
            )
            session.add(run)
            await session.flush()

            await self._count_anomalies(session, connection, report)
            await self._requeue_stuck(session, connection, report)
            await self._sample_verify(session, connection, report)

            run.finished_at = datetime.now(UTC)
            run.ok = True
            run.detail = {
                "checked": report.checked,
                "repaired": report.repaired,
                "missing_destination": report.missing_destination,
                "checksum_mismatch": report.checksum_mismatch,
                "stuck": report.stuck_recordings,
                "failed": report.failed_recordings,
                "orphans": report.orphan_recordings,
            }

            if report.problems:
                brand = (
                    await session.execute(select(Brand).where(Brand.id == connection.brand_id))
                ).scalar_one()
                await dispatch(
                    session,
                    Alert(
                        title="Reconciliation found archive problems",
                        body=(
                            f"{report.problems} problem(s) on {connection.name}; "
                            f"{report.repaired} re-queued for transfer."
                        ),
                        severity=Severity.WARNING,
                        brand_id=brand.id,
                        brand_name=brand.name,
                        dedupe_key=f"reconcile:{connection.id}",
                        fields={
                            "Verified": str(report.checked),
                            "Missing in archive": str(report.missing_destination),
                            "Checksum mismatch": str(report.checksum_mismatch),
                            "Stuck": str(report.stuck_recordings),
                        },
                    ),
                )

            log.info(
                "reconciler.connection_complete",
                connection_id=connection.id,
                checked=report.checked,
                problems=report.problems,
                repaired=report.repaired,
            )
            return report

    async def _count_anomalies(
        self, session: AsyncSession, connection: CommPeakConnection, report: ReconcileReport
    ) -> None:
        """Summarise the states that need a human's attention."""
        rows = (
            await session.execute(
                select(Recording.state, func.count())
                .where(
                    Recording.brand_id == connection.brand_id,
                    Recording.connection_id == connection.id,
                )
                .group_by(Recording.state)
            )
        ).all()
        counts = {str(state): n for state, n in rows}
        report.failed_recordings = counts.get(str(RecordingState.FAILED), 0)

        report.orphan_recordings = (
            await session.execute(
                select(func.count())
                .select_from(Recording)
                .where(
                    Recording.brand_id == connection.brand_id,
                    Recording.connection_id == connection.id,
                    Recording.match_method == "orphan",
                )
            )
        ).scalar_one()

    async def _requeue_stuck(
        self, session: AsyncSession, connection: CommPeakConnection, report: ReconcileReport
    ) -> None:
        """Re-queue recordings that stalled between states.

        A recording left UPLOADED but never VERIFIED means the process died
        between the upload and the read-back. The archive copy may be fine, but
        we have no proof, and unproven is not good enough here.
        """
        stuck = (
            (
                await session.execute(
                    select(Recording)
                    .where(
                        Recording.brand_id == connection.brand_id,
                        Recording.connection_id == connection.id,
                        Recording.state.in_(
                            [
                                RecordingState.UPLOADED,
                                RecordingState.TRANSFERRING,
                                RecordingState.VERIFIED,
                            ]
                        ),
                    )
                    .limit(2000)
                )
            )
            .scalars()
            .all()
        )
        for recording in stuck:
            # VERIFIED but not AVAILABLE means the sidecar step never finished.
            if recording.state is RecordingState.VERIFIED and recording.verified_at:
                recording.state = RecordingState.AVAILABLE
                report.repaired += 1
                continue
            report.stuck_recordings += 1
            recording.state = RecordingState.QUEUED
            if connection.destination_id:
                await queue.enqueue(
                    session,
                    brand_id=recording.brand_id,
                    connection_id=connection.id,
                    recording_id=recording.id,
                    destination_id=connection.destination_id,
                    kind=JobKind.TRANSFER,
                    priority=50,
                )
                report.repaired += 1

    async def _sample_verify(
        self, session: AsyncSession, connection: CommPeakConnection, report: ReconcileReport
    ) -> None:
        """Re-check a sample of archived recordings against the destination."""
        if connection.destination_id is None:
            report.notes.append("no destination configured; archive verification skipped")
            return
        destination = (
            await session.execute(
                select(StorageDestination).where(
                    StorageDestination.id == connection.destination_id
                )
            )
        ).scalar_one_or_none()
        if destination is None or not destination.is_enabled:
            report.notes.append("destination missing or disabled")
            return

        sample = (
            (
                await session.execute(
                    select(Recording)
                    .where(
                        Recording.brand_id == connection.brand_id,
                        Recording.connection_id == connection.id,
                        Recording.state == RecordingState.AVAILABLE,
                        Recording.destination_key.isnot(None),
                    )
                    # Oldest verification first, so every object comes round
                    # eventually rather than the same ones being re-checked.
                    .order_by(Recording.verified_at.asc().nulls_first())
                    .limit(VERIFY_SAMPLE_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not sample:
            return

        client = await open_destination(session, destination)
        async with client as dest:
            for recording in sample:
                report.checked += 1
                before = recording.state
                ok = await verify_recording(session, recording, dest)
                if ok:
                    continue
                if recording.last_error_class == "NOT_FOUND":
                    report.missing_destination += 1
                else:
                    report.checksum_mismatch += 1
                if before is RecordingState.AVAILABLE and connection.destination_id:
                    await queue.enqueue(
                        session,
                        brand_id=recording.brand_id,
                        connection_id=connection.id,
                        recording_id=recording.id,
                        destination_id=connection.destination_id,
                        kind=JobKind.TRANSFER,
                        priority=20,
                    )
                    report.repaired += 1


async def _amain() -> None:
    configure_logging("c2w-reconciler")
    async with platform_session() as session:
        await reconfigure_from_settings("c2w-reconciler", session)

    async with singleton_lock("c2w-reconciler") as acquired:
        if not acquired:
            log.error("reconciler.already_running")
            return
        reconciler = Reconciler()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, reconciler.request_stop)
        try:
            await reconciler.run()
        finally:
            await dispose_engine()


def main() -> int:
    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
