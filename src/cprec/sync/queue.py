"""Job queue on PostgreSQL.

There is no Redis and no broker: the deployment forbids extra daemons, and
using the database we already run buys something a broker cannot give us --
a job's state transition and the recording row it describes commit in the same
transaction.  A worker that dies mid-write can never leave a recording marked
``AVAILABLE`` with its job still ``PENDING``, or vice versa.

Claiming uses ``FOR UPDATE SKIP LOCKED``, which lets N workers pull disjoint
batches with no coordination and no lost work.  Leases rather than locks handle
the crash case: a claimed job whose worker vanished becomes claimable again
once its lease expires, so nothing is stranded and nothing needs a janitor
process to notice.
"""

from __future__ import annotations

import os
import random
import socket
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cprec.db.base import JobKind, JobState, RecordingState
from cprec.db.models.core import Recording, TransferAttempt, TransferJob
from cprec.storage.errors import ErrorClass

__all__ = [
    "backoff_delay",
    "claim_batch",
    "complete",
    "enqueue",
    "fail",
    "queue_depth",
    "reclaim_expired",
    "release",
    "worker_identity",
]


def worker_identity(suffix: str | None = None) -> str:
    """Stable, human-meaningful worker id, recorded on each claim.

    Includes the hostname and pid so an operator reading ``claimed_by`` in the
    database can go straight to the right journal on the right host.
    """
    base = f"{socket.gethostname()}:{os.getpid()}"
    return f"{base}:{suffix}" if suffix else base


def backoff_delay(attempt: int, ladder: Sequence[int]) -> timedelta:
    """Delay before the next attempt, with jitter.

    Jitter matters more than it looks: without it, a CommPeak or Wasabi blip
    that fails 200 jobs at once would retry all 200 simultaneously 30 seconds
    later, reproducing the overload that caused the failure.
    """
    if not ladder:
        return timedelta(seconds=60)
    index = min(max(attempt - 1, 0), len(ladder) - 1)
    base = ladder[index]
    return timedelta(seconds=base * random.uniform(0.8, 1.3))  # noqa: S311 - jitter, not crypto


async def enqueue(
    session: AsyncSession,
    *,
    brand_id: int,
    connection_id: int,
    recording_id: int,
    destination_id: int | None,
    kind: JobKind = JobKind.TRANSFER,
    priority: int = 100,
    run_at: datetime | None = None,
) -> None:
    """Add a job, or leave the existing one alone.

    Idempotent on ``(brand, recording, kind)`` so a re-run of an inventory scan
    cannot queue an object twice -- at 19M objects, "mostly idempotent" would
    mean millions of duplicate transfers.
    """
    await session.execute(
        text(
            """
            INSERT INTO transfer_jobs
                (brand_id, connection_id, destination_id, recording_id, kind,
                 state, priority, next_attempt_at)
            VALUES
                (:brand_id, :connection_id, :destination_id, :recording_id, :kind,
                 'PENDING', :priority, COALESCE(:run_at, now()))
            ON CONFLICT (brand_id, recording_id, kind) DO NOTHING
            """
        ),
        {
            "brand_id": brand_id,
            "connection_id": connection_id,
            "destination_id": destination_id,
            "recording_id": recording_id,
            "kind": str(kind),
            "priority": priority,
            "run_at": run_at,
        },
    )


async def claim_batch(
    session: AsyncSession,
    *,
    worker: str,
    limit: int,
    lease_seconds: int,
    kinds: Sequence[JobKind] | None = None,
    brand_ids: Sequence[int] | None = None,
) -> list[TransferJob]:
    """Atomically claim up to ``limit`` runnable jobs.

    The selection sits in a CTE, and this is load-bearing rather than
    stylistic.  Expressing it as ``UPDATE ... WHERE id IN (SELECT ... LIMIT n
    FOR UPDATE SKIP LOCKED)`` lets PostgreSQL plan the subquery as a semi-join
    and re-execute it per candidate row; each execution skips the rows already
    locked and returns a *different* set, so a worker asking for 3 jobs can walk
    away with every job in the queue.  That silently defeats the concurrency
    caps.  A CTE is evaluated once, so the limit means what it says.

    ``SKIP LOCKED`` is what makes this safe to run from many workers at once:
    each takes a disjoint set instead of blocking on the same head-of-queue row.
    """
    conditions = ["state = 'PENDING'", "next_attempt_at <= now()"]
    params: dict[str, object] = {"worker": worker, "limit": limit}
    if kinds:
        conditions.append("kind = ANY(:kinds)")
        params["kinds"] = [str(k) for k in kinds]
    if brand_ids:
        conditions.append("brand_id = ANY(:brand_ids)")
        params["brand_ids"] = list(brand_ids)

    claimed_ids = (
        (
            await session.execute(
                text(
                    f"""
                    WITH claimed AS (
                        SELECT id
                        FROM transfer_jobs
                        WHERE {" AND ".join(conditions)}
                        -- Priority first so a targeted backfill of the current
                        -- month drains ahead of years of history; then oldest
                        -- first, so nothing starves.
                        ORDER BY priority ASC, next_attempt_at ASC
                        LIMIT :limit
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE transfer_jobs AS j
                    SET state = 'RUNNING',
                        claimed_by = :worker,
                        claimed_at = now(),
                        attempts = j.attempts + 1
                    FROM claimed c
                    WHERE j.id = c.id
                    RETURNING j.id
                    """  # noqa: S608 - conditions are fixed literals, values are bound
                ),
                params,
            )
        )
        .scalars()
        .all()
    )
    if not claimed_ids:
        return []

    jobs = (
        (await session.execute(select(TransferJob).where(TransferJob.id.in_(claimed_ids))))
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for job in jobs:
        session.add(
            TransferAttempt(job_id=job.id, brand_id=job.brand_id, worker=worker, started_at=now)
        )
    await session.flush()
    return list(jobs)


async def complete(
    session: AsyncSession,
    job: TransferJob,
    *,
    bytes_transferred: int = 0,
) -> None:
    """Mark a job done and close its latest attempt."""
    now = datetime.now(UTC)
    job.state = JobState.DONE
    job.finished_at = now
    job.error_class = None
    job.error_detail = None
    await _close_attempt(session, job, ended_at=now, bytes_transferred=bytes_transferred)


async def fail(
    session: AsyncSession,
    job: TransferJob,
    *,
    error_class: ErrorClass,
    detail: str,
    max_attempts: int,
    ladder: Sequence[int],
    bytes_transferred: int = 0,
) -> bool:
    """Record a failure and decide whether to retry.

    Returns True when the job will be retried.  Non-retryable classes -- a bad
    credential, a missing ACL entry -- go straight to FAILED: burning four more
    attempts on an ``AccessDenied`` only delays the alert that a human needs to
    see.
    """
    now = datetime.now(UTC)
    retryable = error_class.retryable and job.attempts < max_attempts

    job.error_class = str(error_class)
    job.error_detail = detail[:4000]
    if retryable:
        job.state = JobState.PENDING
        job.next_attempt_at = now + backoff_delay(job.attempts, ladder)
        job.claimed_by = None
        job.claimed_at = None
    else:
        job.state = JobState.FAILED
        job.finished_at = now

    await _close_attempt(
        session,
        job,
        ended_at=now,
        bytes_transferred=bytes_transferred,
        error_class=str(error_class),
        error_detail=detail[:4000],
    )
    return retryable


async def release(session: AsyncSession, job: TransferJob, *, delay_seconds: int = 0) -> None:
    """Put a claimed job back without counting it as a failure.

    Used when a worker declines a job for a reason that is not the job's fault
    -- a concurrency cap reached, a destination temporarily disabled.
    """
    job.state = JobState.PENDING
    job.claimed_by = None
    job.claimed_at = None
    job.attempts = max(job.attempts - 1, 0)
    job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
    await _close_attempt(session, job, ended_at=datetime.now(UTC), error_detail="released")


async def reclaim_expired(session: AsyncSession, *, lease_seconds: int) -> int:
    """Return jobs whose worker died back to the queue.

    A worker killed mid-transfer leaves its job RUNNING forever otherwise.  The
    lease means recovery needs no external supervisor: the next worker to poll
    picks the job up.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=lease_seconds)
    result = await session.execute(
        update(TransferJob)
        .where(TransferJob.state == JobState.RUNNING, TransferJob.claimed_at < cutoff)
        .values(state=JobState.PENDING, claimed_by=None, claimed_at=None)
        .returning(TransferJob.id)
    )
    ids = result.scalars().all()
    if ids:
        # The recording was left mid-flight; put it back to QUEUED so its state
        # matches the job that will run again.
        await session.execute(
            update(Recording)
            .where(
                Recording.state == RecordingState.TRANSFERRING,
                Recording.id.in_(
                    select(TransferJob.recording_id).where(TransferJob.id.in_(ids))
                ),
            )
            .values(state=RecordingState.QUEUED)
        )
    return len(ids)


async def queue_depth(session: AsyncSession, *, brand_id: int | None = None) -> dict[str, int]:
    """Counts by state, for the dashboard and for alert thresholds."""
    stmt = select(TransferJob.state, func.count()).group_by(TransferJob.state)
    if brand_id is not None:
        stmt = stmt.where(TransferJob.brand_id == brand_id)
    rows = (await session.execute(stmt)).all()
    depth = {str(state): count for state, count in rows}
    for state in JobState:
        depth.setdefault(str(state), 0)
    return depth


async def _close_attempt(
    session: AsyncSession,
    job: TransferJob,
    *,
    ended_at: datetime,
    bytes_transferred: int = 0,
    error_class: str | None = None,
    error_detail: str | None = None,
) -> None:
    """Fill in the open attempt row for this job, if there is one."""
    attempt = (
        await session.execute(
            select(TransferAttempt)
            .where(TransferAttempt.job_id == job.id, TransferAttempt.ended_at.is_(None))
            .order_by(TransferAttempt.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if attempt is None:
        return
    attempt.ended_at = ended_at
    attempt.bytes_transferred = bytes_transferred
    attempt.error_class = error_class
    attempt.error_detail = error_detail


async def pending_for_connection(session: AsyncSession, connection_id: int) -> int:
    """How much work is outstanding for one connection."""
    return (
        await session.execute(
            select(func.count())
            .select_from(TransferJob)
            .where(
                TransferJob.connection_id == connection_id,
                or_(TransferJob.state == JobState.PENDING, TransferJob.state == JobState.RUNNING),
            )
        )
    ).scalar_one()
