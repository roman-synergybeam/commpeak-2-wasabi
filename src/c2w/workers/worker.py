"""Transfer worker.

Claims jobs from the PostgreSQL queue and runs them.  Several instances run as
``c2w-worker@1..N``; they need no coordination because claiming uses
``FOR UPDATE SKIP LOCKED``.

The loop deliberately does very little itself.  Everything about *how* a
transfer behaves -- concurrency, bandwidth, retries -- is read from the database
on each pass, so an operator changing a cap in the UI takes effect within
seconds across every worker without a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.alerts.base import Alert, Severity, dispatch
from c2w.db.base import JobKind, JobState, RecordingState
from c2w.db.models.core import (
    Brand,
    CommPeakConnection,
    Recording,
    StorageDestination,
    Tenant,
    TransferJob,
)
from c2w.db.session import dispose_engine, platform_session
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.settings import settings_service
from c2w.storage.errors import TransferError, classify_exception
from c2w.storage.factory import open_destination, open_source
from c2w.storage.s3_adapter import RateLimiter
from c2w.sync import queue
from c2w.sync.transfer import transfer_recording

log = get_logger(__name__)

IDLE_SLEEP_SECONDS = 5.0
DISABLED_SLEEP_SECONDS = 30.0


class Worker:
    def __init__(self, worker_id: str) -> None:
        self.identity = queue.worker_identity(worker_id)
        self._stopping = asyncio.Event()
        # One limiter for the process, so the configured ceiling is a real
        # ceiling rather than a per-job allowance.
        self._limiter: RateLimiter | None = None
        self._limiter_mbps: float | None = None

    def request_stop(self) -> None:
        self._stopping.set()

    async def _limiter_for(self, session: AsyncSession) -> RateLimiter | None:
        mbps = await settings_service.get_float(session, "transfer.bandwidth_limit_mbps")
        if mbps <= 0:
            self._limiter, self._limiter_mbps = None, 0.0
            return None
        if self._limiter is None or self._limiter_mbps != mbps:
            self._limiter = RateLimiter.from_mbps(mbps)
            self._limiter_mbps = mbps
        return self._limiter

    async def run(self) -> None:
        log.info("worker.started", worker=self.identity)
        while not self._stopping.is_set():
            try:
                slept = await self._tick()
            except Exception as exc:
                log.exception("worker.tick_failed", error=str(exc))
                slept = IDLE_SLEEP_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=slept)
        log.info("worker.stopped", worker=self.identity)

    async def _tick(self) -> float:
        """One pass: reclaim, claim, run.  Returns how long to sleep after."""
        async with platform_session() as session:
            if not await settings_service.get_bool(session, "transfer.enabled"):
                return DISABLED_SLEEP_SECONDS

            lease = await settings_service.get_int(session, "transfer.job_lease_seconds")
            batch = await settings_service.get_int(session, "transfer.job_claim_batch")
            global_cap = await settings_service.get_int(session, "transfer.concurrency_global")
            # Read before the claim, because the claim needs it: the share per
            # account is derived from the per-account cap.
            per_conn = await settings_service.get_int(session, "source.concurrency_per_connection")

            # A worker that died leaves its jobs RUNNING; the lease is what
            # makes recovery automatic rather than needing a janitor.
            if reclaimed := await queue.reclaim_expired(session, lease_seconds=lease):
                log.info("worker.reclaimed_expired", jobs=reclaimed)

            # One claim per account, rather than one claim off the head of a
            # global queue.
            #
            # This is worth the extra statements. Inventory enqueues an
            # account's objects in bulk, so a single claim ordered by
            # `priority, next_attempt_at` returns consecutive jobs -- in
            # practice all from one account -- and the per-account cap then
            # runs them one at a time. Measured on the live backlog: five
            # accounts holding 120,000 queued jobs were idle while everything
            # piled onto one, using about five of the forty concurrent slots
            # eight accounts at five each allow.
            #
            # Asking per account cannot be folded into one query: ranking
            # accounts with a window function forces the row locking into an
            # outer step, and `SKIP LOCKED` then stops skipping *while*
            # selecting, so two workers pick the same head rows and the second
            # gets nothing. See claim_batch.
            share = max(2, per_conn * 2)
            jobs: list[TransferJob] = []
            for connection_id in await self._connections_with_work(session):
                if len(jobs) >= min(batch, global_cap):
                    break
                jobs.extend(
                    await queue.claim_batch(
                        session,
                        worker=self.identity,
                        limit=share,
                        lease_seconds=lease,
                        kinds=[JobKind.TRANSFER],
                        connection_ids=[connection_id],
                    )
                )
            if not jobs:
                return IDLE_SLEEP_SECONDS

            limiter = await self._limiter_for(session)
            per_brand = await settings_service.get_int(session, "transfer.concurrency_per_brand")

        # Group by connection so the per-connection cap is honoured; CommPeak
        # throttles above about 5 concurrent transfers per account.
        by_connection: dict[int, list[TransferJob]] = defaultdict(list)
        for job in jobs:
            by_connection[job.connection_id].append(job)

        brand_gates: dict[int, asyncio.Semaphore] = {}
        for job in jobs:
            brand_gates.setdefault(job.brand_id, asyncio.Semaphore(per_brand))

        await asyncio.gather(
            *(
                self._run_connection_group(group, per_conn, brand_gates, limiter)
                for group in by_connection.values()
            )
        )
        return 0.0

    @staticmethod
    async def _connections_with_work(session: AsyncSession) -> list[int]:
        """Accounts that have runnable jobs, fewest queued first.

        Fewest first so a small account is not permanently behind a large one:
        `go4rex.td` has 52,000 queued and would otherwise be claimed from on
        every pass while an account with 300 waited for it to finish.

        Cheap enough to run each pass -- it is an index scan over PENDING and
        there are eight accounts -- and reading it fresh is what lets a worker
        notice an account whose access has just come back.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT connection_id
                    FROM transfer_jobs
                    WHERE state = 'PENDING' AND next_attempt_at <= now()
                    GROUP BY connection_id
                    ORDER BY count(*) ASC
                    """
                )
            )
        ).scalars().all()
        return [int(r) for r in rows]

    async def _run_connection_group(
        self,
        jobs: list[TransferJob],
        per_connection: int,
        brand_gates: dict[int, asyncio.Semaphore],
        limiter: RateLimiter | None,
    ) -> None:
        gate = asyncio.Semaphore(per_connection)

        async def run_one(job: TransferJob) -> None:
            async with gate, brand_gates[job.brand_id]:
                await self._run_job(job.id, limiter)

        await asyncio.gather(*(run_one(job) for job in jobs))

    async def _run_job(self, job_id: int, limiter: RateLimiter | None) -> None:
        """Run one transfer in its own transaction.

        Each job commits independently so a failure never rolls back the work
        of its neighbours.
        """
        async with platform_session() as session:
            job = (
                await session.execute(select(TransferJob).where(TransferJob.id == job_id))
            ).scalar_one_or_none()
            if job is None or job.state is not JobState.RUNNING:
                return

            max_attempts = await settings_service.get_int(session, "transfer.max_attempts")
            ladder = await settings_service.get(session, "transfer.retry_backoff_seconds")
            threshold = await settings_service.get_int(
                session, "transfer.multipart_threshold_bytes"
            )
            chunk = await settings_service.get_int(session, "transfer.multipart_chunk_bytes")

            try:
                # The tenant is still loaded by _load_context -- it is part of
                # the job's context and other callers use it -- but the archive
                # path no longer needs it: the folder is the account name.
                recording, connection, destination, brand, _tenant = await self._load_context(
                    session, job
                )
            except LookupError as exc:
                await queue.fail(
                    session,
                    job,
                    error_class=classify_exception(exc).error_class,
                    detail=str(exc),
                    max_attempts=max_attempts,
                    ladder=ladder,
                )
                return

            write_sidecar = await settings_service.get_bool(
                session, "transfer.write_sidecar_metadata", brand_id=brand.id
            )

            try:
                source_client = await open_source(session, connection, limiter=limiter)
                dest_client = await open_destination(session, destination)
                async with source_client as src, dest_client as dst:
                    outcome = await transfer_recording(
                        session,
                        recording,
                        destination,
                        src,
                        dst,
                        # The CommPeak account name, so the archive's top
                        # level reads as the list of accounts it holds.
                        account=connection.name,
                        multipart_threshold=threshold,
                        multipart_chunk=chunk,
                        write_sidecar=write_sidecar,
                    )
                await queue.complete(session, job, bytes_transferred=outcome.bytes_transferred)
                if outcome.verified:
                    destination.bytes_stored += outcome.bytes_transferred
                    destination.objects_stored += 1

            except Exception as exc:
                error = exc if isinstance(exc, TransferError) else classify_exception(exc)
                recording.state = RecordingState.QUEUED
                recording.last_error_class = str(error.error_class)
                recording.last_error_detail = error.message[:4000]

                will_retry = await queue.fail(
                    session,
                    job,
                    error_class=error.error_class,
                    detail=str(error),
                    max_attempts=max_attempts,
                    ladder=ladder,
                )
                if not will_retry:
                    recording.state = RecordingState.FAILED

                log.warning(
                    "worker.job_failed",
                    job_id=job.id,
                    recording_id=recording.id,
                    error_class=str(error.error_class),
                    retrying=will_retry,
                    error=error.message,
                )
                # A credential or ACL problem will fail every job for this
                # connection, so it needs a human now rather than a growing
                # pile of retries.
                if error.error_class.alert_immediately:
                    await dispatch(
                        session,
                        Alert(
                            title=f"{error.error_class} on {connection.name}",
                            body=error.message,
                            severity=Severity.CRITICAL,
                            brand_id=brand.id,
                            brand_name=brand.name,
                            dedupe_key=f"{error.error_class}:{connection.id}",
                            fields={
                                "Connection": connection.name,
                                "Bucket": connection.s3_bucket,
                                **({"Fix": error.hint} if error.hint else {}),
                            },
                        ),
                    )

    async def _load_context(
        self, session: AsyncSession, job: TransferJob
    ) -> tuple[Recording, CommPeakConnection, StorageDestination, Brand, Tenant]:
        recording = (
            await session.execute(
                select(Recording).where(
                    Recording.brand_id == job.brand_id, Recording.id == job.recording_id
                )
            )
        ).scalar_one_or_none()
        if recording is None:
            raise LookupError(f"recording {job.recording_id} no longer exists")

        connection = (
            await session.execute(
                select(CommPeakConnection).where(CommPeakConnection.id == job.connection_id)
            )
        ).scalar_one_or_none()
        if connection is None:
            raise LookupError(f"connection {job.connection_id} no longer exists")

        destination_id = job.destination_id or recording.destination_id or connection.destination_id
        destination = (
            await session.execute(
                select(StorageDestination).where(StorageDestination.id == destination_id)
            )
        ).scalar_one_or_none()
        if destination is None:
            raise LookupError("no archive destination is configured for this recording")
        if not destination.is_enabled:
            raise LookupError(f"destination {destination.name} is disabled")

        brand = (
            await session.execute(select(Brand).where(Brand.id == job.brand_id))
        ).scalar_one()
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == recording.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            raise LookupError(f"tenant {recording.tenant_id} no longer exists")
        return recording, connection, destination, brand, tenant


async def _amain() -> None:
    configure_logging("c2w-worker")
    async with platform_session() as session:
        await reconfigure_from_settings("c2w-worker", session)

    import os

    worker = Worker(os.environ.get("C2W_WORKER_ID", "1"))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Graceful stop matters here: a worker killed mid-upload must be allowed
        # to abort its multipart upload, or the abandoned parts keep costing
        # money at the destination until a lifecycle rule clears them.
        loop.add_signal_handler(sig, worker.request_stop)
    try:
        await worker.run()
    finally:
        await dispose_engine()


def main() -> int:
    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
