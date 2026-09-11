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
from typing import cast

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
from c2w.storage.commpeak import CommPeakSource
from c2w.storage.errors import ErrorClass, TransferError, classify_exception
from c2w.storage.factory import open_destination, open_source
from c2w.storage.s3_adapter import RateLimiter, S3Client
from c2w.storage.wasabi import WasabiDestination
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
        #: Rotates which account is claimed from first. See
        #: `_connections_with_work` for why this replaced an ORDER BY count(*).
        self._pass = 0
        #: Entered S3 clients, reused across jobs. See `_client_for`.
        self._clients: dict[tuple[str, int, object], S3Client] = {}
        #: Clients taken out of service but not yet closed. See `_retire`.
        self._retired: list[S3Client] = []

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
        await self._close_clients()
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
            # How many to take per account, derived from the caps the
            # operator already set rather than from a magic multiplier.
            #
            # This was `max(2, per_conn * 2)`, which with the live setting of
            # one concurrent transfer per account meant **two jobs per account
            # per pass**. A pass therefore transferred a dozen objects and
            # then stopped to re-claim, and the measured result was sixteen
            # transfers in flight out of the thirty those accounts allow --
            # roughly half the duty cycle spent claiming rather than copying.
            #
            # Filling the claim batch across the accounts that actually have
            # work keeps the pipeline full between claims. It does **not**
            # raise how many run at once against any one account: that is
            # still gated by `source.concurrency_per_connection` inside
            # `_run_connection_group`, so CommPeak sees exactly what it did
            # before. More jobs per pass, same concurrency.
            accounts = await self._connections_with_work(session)
            ceiling = min(batch, global_cap)
            share = max(per_conn * 2, ceiling // max(1, len(accounts)))
            jobs: list[TransferJob] = []
            for connection_id in accounts:
                if len(jobs) >= ceiling:
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
        # Every transfer in the batch has finished, so anything retired during
        # it is genuinely unused now and can be closed.
        await self._close_retired()
        return 0.0

    async def _connections_with_work(self, session: AsyncSession) -> list[int]:
        """Accounts that have runnable jobs, in a rotating order.

        Reading it fresh each pass is what lets a worker notice an account
        whose access has just come back.

        **This used to order by `count(*)` and claimed to be cheap.** It was
        not: `GROUP BY connection_id ORDER BY count(*)` over nine million
        pending rows counts every one of them, so each worker ran a full
        aggregate of the queue on every pass -- five workers, several times a
        minute, and it was one of the two things keeping PostgreSQL at four of
        this host's eight cores. The docstring asserting it was an index scan
        was written by inspection rather than by measurement, which is how it
        survived.

        Fairness does not need those counts. Every account with work is
        claimed from on every pass anyway, so the order only decides who is
        served when a pass hits its batch ceiling -- and rotating the list by a
        per-worker counter guarantees no account is systematically last, which
        is a stronger guarantee than "fewest queued first" gave and costs
        nothing. What remains in the database is one index probe per account
        against `ix_transfer_jobs_claim`.
        """
        rows = (
            await session.execute(
                text(
                    """
                    SELECT c.id
                    FROM commpeak_connections c
                    WHERE c.is_enabled
                      AND EXISTS (
                          SELECT 1 FROM transfer_jobs j
                          WHERE j.connection_id = c.id
                            AND j.state = 'PENDING'
                            AND j.next_attempt_at <= now()
                      )
                    ORDER BY c.id
                    """
                )
            )
        ).scalars().all()
        ids = [int(r) for r in rows]
        if not ids:
            return ids
        self._pass += 1
        offset = self._pass % len(ids)
        return ids[offset:] + ids[:offset]

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

    async def _source_for(
        self,
        connection: CommPeakConnection,
        limiter: RateLimiter | None,
        session: AsyncSession,
    ) -> CommPeakSource:
        """The cached read-only client for one CommPeak account."""
        return cast(
            CommPeakSource, await self._client_for("source", connection, limiter, session)
        )

    async def _destination_for(
        self,
        destination: StorageDestination,
        limiter: RateLimiter | None,
        session: AsyncSession,
    ) -> WasabiDestination:
        """The cached client for one archive destination."""
        return cast(
            WasabiDestination, await self._client_for("dest", destination, limiter, session)
        )

    async def _client_for(
        self,
        kind: str,
        row: CommPeakConnection | StorageDestination,
        limiter: RateLimiter | None,
        session: AsyncSession,
    ) -> S3Client:
        """An open S3 client for one account or destination, reused across jobs.

        This exists because building them per job was the single largest
        consumer of CPU on the host. Measured, per client:

            unsealing the credentials      14 ms of CPU
            creating the botocore client   87-99 ms of CPU

        `_run_job` opened one of each, so **roughly 214 ms of CPU went on
        setup for every object copied** -- and at eight objects a second that
        is over 1.5 cores spent building clients that are identical to the ones
        thrown away a moment earlier. The files average 0.73 MB; the setup cost
        far exceeded the copy.

        Reuse is what aiobotocore is designed for: the client owns a connection
        pool, and discarding it also discarded every established TLS
        connection, so each object paid for a fresh handshake too.

        Keyed on `updated_at` as well as the id, so editing a credential in
        the UI produces a *new* client rather than one that keeps using the old
        secret until the process restarts -- and the superseded one is closed
        rather than leaked. Evicted on network errors as well, in
        `_evict_clients`, because a pool that has gone bad should not be
        retried for ever.
        """
        key = (kind, row.id, row.updated_at)
        existing = self._clients.get(key)
        if existing is not None:
            return existing

        for stale in [k for k in self._clients if k[0] == kind and k[1] == row.id]:
            self._retire(self._clients.pop(stale))

        if kind == "source":
            client: S3Client = await open_source(
                session, cast(CommPeakConnection, row), limiter=limiter
            )
        else:
            client = await open_destination(
                session, cast(StorageDestination, row), limiter=limiter
            )
        opened = await client.__aenter__()
        self._clients[key] = opened
        return opened

    def _retire(self, client: S3Client) -> None:
        """Take a client out of service without closing it yet.

        Closing it here would be a bug, and was one. A cached client is
        **shared** by every transfer running against that account -- up to five
        concurrently -- and `S3Client.__aexit__` sets its internal client to
        None. So closing one mid-flight made every other transfer already
        using it raise `S3Client used outside its async context manager`,
        reported as `CONFIG_ERROR` with a hint about endpoint addressing that
        had nothing to do with it. One genuine network error turned into
        several spurious failures on the same account, and thirteen jobs failed
        that way before it was caught.

        So eviction only removes it from the cache -- new jobs immediately get
        a fresh one -- and the close happens in `_close_retired`, which runs
        after the batch has finished and nothing can still be holding it.
        """
        self._retired.append(client)

    async def _close_retired(self) -> None:
        """Close clients retired during this pass. Safe only once every
        transfer in the batch has finished."""
        while self._retired:
            client = self._retired.pop()
            with contextlib.suppress(Exception):
                await client.__aexit__(None, None, None)

    def _evict_clients(self, *ids: int) -> None:
        """Stop handing out the cached clients for these rows."""
        for key in [k for k in self._clients if k[1] in ids]:
            self._retire(self._clients.pop(key))

    async def _close_clients(self) -> None:
        """Close every client. A multipart upload in flight is aborted by the
        transfer code itself; this only releases the pools."""
        for key in list(self._clients):
            self._retire(self._clients.pop(key))
        await self._close_retired()

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
                # Reused, not opened per job -- see `_client_for`. They
                # deliberately outlive this block rather than sitting in an
                # `async with`: closing them per job is what cost 214 ms of
                # CPU per object.
                src = await self._source_for(connection, limiter, session)
                dst = await self._destination_for(destination, limiter, session)
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
                # The archive's totals are deliberately **not** maintained
                # here. This used to do `destination.bytes_stored += n`, which
                # was wrong twice over: it is a read-modify-write of a value
                # loaded earlier in this session, so two workers finishing at
                # once each wrote their own increment and one was lost; and
                # every transfer took a row lock on the *same* destination
                # row, so forty concurrent transfers queued up behind one
                # counter -- visible as `Lock` waits on
                # `UPDATE storage_destinations` in `pg_stat_activity`.
                #
                # The numbers are derivable from `recordings`, which is where
                # the truth already lives, so the scheduler recomputes them
                # periodically instead. A counter that is contended *and*
                # drifting is worse than one that is a minute old.

            except Exception as exc:
                error = exc if isinstance(exc, TransferError) else classify_exception(exc)
                if error.error_class in (ErrorClass.NETWORK_ERROR, ErrorClass.AUTH_ERROR):
                    # A dead connection pool, or credentials that have been
                    # changed underneath us. Either way the cached client is
                    # not worth keeping; the retry builds a new one.
                    self._evict_clients(connection.id, destination.id)
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
