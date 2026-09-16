"""When each CommPeak account will be fully archived.

The platform-wide figure hides the thing an operator actually needs: the
estate finishes when its *slowest* account finishes, and two of the eight hold
nearly all the work. A single date invites the reading that everything lands
together, when in practice six accounts complete in days and two take weeks.

Cached, because it has to be. The two aggregates behind it take about ten
seconds each across twenty-three million recordings, and the dashboard
refreshes every ten -- so uncached this would be the most expensive thing on
the page by an order of magnitude and would never finish before the next poll
asked again. Keyed by the RLS scope, like the other dashboard caches, so one
organisation's figures can never be served to another.

The same honesty rule as the platform-wide panel: recordings keep arriving, so
an account copying no faster than its own inflow never finishes, and no date
is given for it rather than a flattering one.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger

__all__ = ["AccountEta", "account_etas", "forget_cached_etas"]

log = get_logger(__name__)

#: Process-local, like the dashboard's other caches. A restart costs one
#: recomputation.
_CACHE: dict[str, tuple[float, list[AccountEta]]] = {}

#: Scopes with a refresh already in flight, so an expiring cache starts
#: one recomputation rather than one per waiting request.
_REFRESHING: set[str] = set()


def forget_cached_etas() -> None:
    _CACHE.clear()


@dataclass(slots=True)
class AccountEta:
    account: str
    indexed: int
    verified: int
    rate_per_day: int
    inflow_per_day: int

    @property
    def remaining(self) -> int:
        return max(0, self.indexed - self.verified)

    @property
    def percent(self) -> float:
        if not self.indexed:
            return 0.0
        return round(100.0 * self.verified / self.indexed, 1)

    @property
    def net_per_day(self) -> int:
        return self.rate_per_day - self.inflow_per_day

    @property
    def done(self) -> bool:
        return self.indexed > 0 and self.remaining == 0

    @property
    def days_remaining(self) -> float | None:
        """None when no honest date can be given."""
        if self.done or self.remaining == 0:
            return None
        if self.net_per_day <= 0:
            return None
        return self.remaining / self.net_per_day

    @property
    def eta(self) -> datetime | None:
        days = self.days_remaining
        return None if days is None else datetime.now(UTC) + timedelta(days=days)

    @property
    def state(self) -> str:
        """One of the kit's four pill meanings."""
        if self.done:
            return "ok"
        if self.rate_per_day <= 0:
            return "idle"
        if self.net_per_day <= 0:
            return "err"        # losing ground: arriving faster than copying
        return "warn"           # in progress


async def account_etas(
    session: AsyncSession, *, cache_seconds: int = 120
) -> list[AccountEta]:
    """Per-account progress and completion date, cached."""
    scope = (
        await session.execute(
            text("SELECT coalesce(current_setting('c2w.brand_id', true), '')")
        )
    ).scalar_one() or "unscoped"

    now = time.monotonic()
    if cache_seconds > 0:
        cached = _CACHE.get(scope)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            # Expired, but stale beats slow here. Recomputing takes about
            # forty seconds across twenty-three million recordings, and the
            # panel holding this refreshes every ten -- so blocking on it would
            # stall the dashboard for every viewer once per cache period, and
            # a figure that moves by hours per day is not worth that. The old
            # answer goes out now and a refresh runs behind it.
            if scope not in _REFRESHING:
                _REFRESHING.add(scope)
                asyncio.get_running_loop().create_task(
                    _refresh(scope, cache_seconds)
                )
            return cached[1]

    return await _compute(session, scope, cache_seconds)


async def _compute(
    session: AsyncSession, scope: str, cache_seconds: int
) -> list[AccountEta]:
    now = time.monotonic()
    totals = {
        r.connection_id: r
        for r in (
            await session.execute(
                text(
                    """
                    SELECT connection_id,
                           count(*) AS indexed,
                           count(*) FILTER (WHERE verified_at IS NOT NULL) AS verified
                    FROM recordings GROUP BY connection_id
                    """
                )
            )
        ).all()
    }
    rates = {
        r.connection_id: int(r.n)
        for r in (
            await session.execute(
                text(
                    """
                    SELECT connection_id, count(*) AS n FROM recordings
                    WHERE verified_at >= now() - interval '24 hours'
                    GROUP BY connection_id
                    """
                )
            )
        ).all()
    }
    inflow = {
        r.connection_id: int(r.n)
        for r in (
            await session.execute(
                text(
                    """
                    SELECT connection_id, count(*) AS n FROM recordings
                    WHERE started_at >= now() - interval '24 hours'
                    GROUP BY connection_id
                    """
                )
            )
        ).all()
    }
    names = {
        r.id: r.name
        for r in (
            await session.execute(
                text("SELECT id, name FROM commpeak_connections WHERE is_enabled")
            )
        ).all()
    }

    out = [
        AccountEta(
            account=name,
            indexed=int(totals[cid].indexed) if cid in totals else 0,
            verified=int(totals[cid].verified) if cid in totals else 0,
            rate_per_day=rates.get(cid, 0),
            inflow_per_day=inflow.get(cid, 0),
        )
        for cid, name in names.items()
    ]
    # Slowest first: the estate finishes when its last account does, so the
    # one that needs attention belongs at the top rather than alphabetically
    # buried.
    out.sort(key=lambda a: (a.done, -(a.days_remaining or 0), a.account))

    if cache_seconds > 0:
        _CACHE[scope] = (now + cache_seconds, out)
    return out


async def _refresh(scope: str, cache_seconds: int) -> None:
    """Recompute one scope's figures on its own session, off the request path."""
    from c2w.db.session import platform_session

    try:
        async with platform_session() as session:
            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"),
                {"b": "" if scope == "unscoped" else scope},
            )
            await _compute(session, scope, cache_seconds)
    except Exception:
        log.warning("account_eta.refresh_failed", scope=scope, exc_info=True)
    finally:
        _REFRESHING.discard(scope)


def as_context(items: list[AccountEta]) -> list[dict[str, Any]]:
    return [
        {
            "account": a.account,
            "indexed": a.indexed,
            "verified": a.verified,
            "remaining": a.remaining,
            "percent": a.percent,
            "rate_per_day": a.rate_per_day,
            "net_per_day": a.net_per_day,
            "days_remaining": a.days_remaining,
            "eta": a.eta,
            "state": a.state,
            "done": a.done,
        }
        for a in items
    ]
