"""When the archive will hold everything CommPeak holds.

The question this answers -- "when are the two sides equal?" -- decides when
deleting at the source can even be contemplated, so it is worth being careful
about what is known and what is guessed.

Three quantities, and only one of them is solid:

* **Archived** is exact. A recording counts only once its copy has been
  verified byte for byte, which is the same bar the rest of the system uses.
* **Indexed** is exact but partial: it is what the inventory has walked so far,
  not what CommPeak holds.
* **The source total is an estimate.** Nothing here can measure it without
  listing eight buckets end to end, so it comes from
  `archive.source_objects_estimate` and every figure derived from it is
  labelled an estimate. This is why the tile says "estimated" rather than
  printing a date and hoping.

And the part that makes a naive ETA wrong: **recordings are still arriving**.
CommPeak takes roughly 35,000 new calls a day on this estate, so the target
moves while you copy towards it. What matters is not the copy rate but the copy
rate *minus* the inflow -- and if that is zero or negative the two sides never
converge, however long you wait. An ETA that ignores inflow would have reported
a comfortable finish date on a day the gap was widening.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["Convergence", "ConvergenceState", "convergence"]

#: Inflow is averaged over this many whole days. Long enough to cover a quiet
#: weekend, short enough to notice a change in call volume.
INFLOW_DAYS = 7


class ConvergenceState(enum.StrEnum):
    """Why there is, or is not, a completion date."""

    #: Copying faster than recordings arrive; a date can be given.
    CONVERGING = "converging"
    #: Copying, but no faster than they arrive. The gap is not closing.
    LOSING = "losing"
    #: Nothing has been copied recently at all.
    STALLED = "stalled"
    #: The archive already holds everything the estimate says exists.
    COMPLETE = "complete"


@dataclass(slots=True)
class Convergence:
    archived: int
    indexed: int
    source_estimate: int
    archived_bytes: int
    #: Recordings verified in the last 24 hours.
    rate_per_day: int
    #: New recordings arriving at the source, per day.
    inflow_per_day: int
    state: ConvergenceState

    @property
    def remaining(self) -> int:
        return max(0, self.source_estimate - self.archived)

    @property
    def net_per_day(self) -> int:
        """Rate at which the gap actually closes."""
        return self.rate_per_day - self.inflow_per_day

    @property
    def percent(self) -> float:
        if self.source_estimate <= 0:
            return 0.0
        return round(100.0 * self.archived / self.source_estimate, 1)

    @property
    def days_remaining(self) -> float | None:
        """None whenever no honest date can be given."""
        if self.state is not ConvergenceState.CONVERGING:
            return None
        return self.remaining / self.net_per_day

    @property
    def eta(self) -> datetime | None:
        days = self.days_remaining
        if days is None:
            return None
        return datetime.now(UTC) + timedelta(days=days)


async def convergence(session: AsyncSession, *, source_estimate: int) -> Convergence:
    """Measure how far the archive is from the source, and whether it is gaining."""
    totals = (
        await session.execute(
            text(
                """
                SELECT count(*) AS indexed,
                       count(*) FILTER (WHERE verified_at IS NOT NULL) AS archived,
                       coalesce(sum(destination_size)
                                FILTER (WHERE verified_at IS NOT NULL), 0) AS bytes
                FROM recordings
                """
            )
        )
    ).mappings().one()

    rate = int(
        (
            await session.execute(
                text(
                    "SELECT count(*) FROM recordings "
                    "WHERE verified_at >= now() - interval '24 hours'"
                )
            )
        ).scalar_one()
    )

    # Averaged over whole days only. Including today would divide a part-day's
    # calls by a whole day and understate the inflow, which flatters the ETA.
    inflow_total = int(
        (
            await session.execute(
                text(
                    """
                    SELECT count(*) FROM recordings
                    WHERE started_at >= date_trunc('day', now()) - make_interval(days => :d)
                      AND started_at <  date_trunc('day', now())
                    """
                ),
                {"d": INFLOW_DAYS},
            )
        ).scalar_one()
    )
    inflow = inflow_total // INFLOW_DAYS

    archived = int(totals["archived"])
    state = ConvergenceState.CONVERGING
    if archived >= source_estimate > 0:
        state = ConvergenceState.COMPLETE
    elif rate <= 0:
        state = ConvergenceState.STALLED
    elif rate - inflow <= 0:
        state = ConvergenceState.LOSING

    return Convergence(
        archived=archived,
        indexed=int(totals["indexed"]),
        source_estimate=source_estimate,
        archived_bytes=int(totals["bytes"]),
        rate_per_day=rate,
        inflow_per_day=inflow,
        state=state,
    )


def merge(parts: list[Convergence]) -> Convergence | None:
    """Add up per-organisation figures for the platform-wide view.

    Counts and rates add. The state is recomputed from the merged rates rather
    than combined from the parts, because "one organisation is stalled and
    another is converging" has no sensible merged state -- only the totals do.
    """
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    archived = sum(p.archived for p in parts)
    rate = sum(p.rate_per_day for p in parts)
    inflow = sum(p.inflow_per_day for p in parts)
    estimate = max(p.source_estimate for p in parts)
    state = ConvergenceState.CONVERGING
    if archived >= estimate > 0:
        state = ConvergenceState.COMPLETE
    elif rate <= 0:
        state = ConvergenceState.STALLED
    elif rate - inflow <= 0:
        state = ConvergenceState.LOSING
    return Convergence(
        archived=archived,
        indexed=sum(p.indexed for p in parts),
        source_estimate=estimate,
        archived_bytes=sum(p.archived_bytes for p in parts),
        rate_per_day=rate,
        inflow_per_day=inflow,
        state=state,
    )


def as_context(c: Convergence | None) -> dict[str, Any]:
    """Flatten for a template, so the page does no arithmetic of its own."""
    if c is None:
        return {}
    return {
        "archived": c.archived,
        "indexed": c.indexed,
        "source_estimate": c.source_estimate,
        "remaining": c.remaining,
        "percent": c.percent,
        "rate_per_day": c.rate_per_day,
        "inflow_per_day": c.inflow_per_day,
        "net_per_day": c.net_per_day,
        "days_remaining": c.days_remaining,
        "eta": c.eta,
        "state": str(c.state),
    }
