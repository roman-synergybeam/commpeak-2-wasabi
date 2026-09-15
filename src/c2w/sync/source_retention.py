"""Choosing which CommPeak objects may be deleted, and refusing to guess.

Every link in this chain has to hold for a single object to be removed, and
each exists because of a specific way this could go wrong:

1. **Delete credentials are configured for the account.** Without them there is
   no object capable of deleting (`CommPeakDeleter`), so nothing else matters.
2. **The account is switched on for deletion**, individually. A decision about
   one bucket, not about the estate.
3. **The account passes the completeness report** -- everything indexed for it
   is verified, and the inventory's coverage is known. See `completeness`.
4. **The recording is older than the retention window**, measured from the
   call, not from when we happened to copy it.
5. **The recording is verified**, with a destination key and a checksum.
6. **The destination object still answers, right now.** This is the one that
   cannot be skipped: `verified_at` says the copy was good *then*, and a
   lifecycle rule, a policy change or a mistaken cleanup would not have told
   us. Deleting the last copy of a call because a three-week-old flag said it
   was safe is the failure this whole module exists to prevent.

A dry run performs every step except the delete, which is what makes it worth
running: it exercises the same selection and the same destination re-check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger

__all__ = ["DeletionPlan", "plan_deletions", "refusal_reasons"]

log = get_logger(__name__)


@dataclass(slots=True)
class DeletionPlan:
    """What would be deleted for one account, and why it may or may not be."""

    connection_id: int
    account: str
    brand_id: int
    retention_days: int
    #: Recordings meeting every database-side condition.
    eligible: int
    #: The oldest and newest call dates in that set, so the operator can see
    #: at a glance whether the window is what they meant.
    oldest: datetime | None
    newest: datetime | None
    bytes_at_source: int
    refusals: list[str] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)

    @property
    def permitted(self) -> bool:
        return not self.refusals and self.eligible > 0


def refusal_reasons(
    *,
    has_delete_credentials: bool,
    account_enabled: bool,
    completeness_blocking: list[str],
    global_enabled: bool,
) -> list[str]:
    """Everything standing between this account and a deletion, in words."""
    reasons: list[str] = []
    if not global_enabled:
        reasons.append("source deletion is switched off for the whole platform")
    if not account_enabled:
        reasons.append("source deletion is not switched on for this account")
    if not has_delete_credentials:
        reasons.append(
            "no delete credentials are configured for this account -- deletion "
            "uses a separate credential pair from the read-only one"
        )
    reasons.extend(completeness_blocking)
    return reasons


async def plan_deletions(
    session: AsyncSession,
    *,
    connection_id: int,
    account: str,
    brand_id: int,
    retention_days: int,
    limit: int = 200,
) -> DeletionPlan:
    """Count and sample what the database considers eligible.

    Deliberately does **not** consult the destination: that check is per object
    and happens at the moment of deleting, because an answer gathered here
    would already be stale by the time the delete ran.

    `started_at` rather than `verified_at` decides the age. Retention is a
    statement about how long a *call* is kept, and copying it late must not
    make it eligible late -- or a backlog would quietly extend everyone's
    retention.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    counts = (
        await session.execute(
            text(
                """
                SELECT count(*) AS eligible,
                       min(started_at) AS oldest,
                       max(started_at) AS newest,
                       coalesce(sum(source_size), 0) AS bytes
                FROM recordings
                WHERE connection_id = :c
                  AND started_at < :cutoff
                  AND verified_at IS NOT NULL
                  AND destination_key IS NOT NULL
                  AND checksum_sha256 IS NOT NULL
                  AND state <> 'SOURCE_DELETED'
                """
            ),
            {"c": connection_id, "cutoff": cutoff},
        )
    ).mappings().one()

    sample = (
        await session.execute(
            text(
                """
                SELECT id, source_key, destination_key, source_size,
                       started_at, verified_at
                FROM recordings
                WHERE connection_id = :c
                  AND started_at < :cutoff
                  AND verified_at IS NOT NULL
                  AND destination_key IS NOT NULL
                  AND checksum_sha256 IS NOT NULL
                  AND state <> 'SOURCE_DELETED'
                ORDER BY started_at
                LIMIT :limit
                """
            ),
            {"c": connection_id, "cutoff": cutoff, "limit": limit},
        )
    ).mappings().all()

    return DeletionPlan(
        connection_id=connection_id,
        account=account,
        brand_id=brand_id,
        retention_days=retention_days,
        eligible=int(counts["eligible"]),
        oldest=counts["oldest"],
        newest=counts["newest"],
        bytes_at_source=int(counts["bytes"]),
        sample=[dict(r) for r in sample],
    )
