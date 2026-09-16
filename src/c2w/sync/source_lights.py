"""A light per CommPeak account: is it answering, or not.

The trap this has to avoid is the one that has already cost this project a
day. `commpeak_connections.status` is the verdict of the **last probe**, not a
live reading, so a row can say `OK` long after access has stopped -- and it
did: every account showed `OK` at 09:17 while every request was being refused.
A dashboard light driven straight off that column would have been green
throughout an outage.

So the light is a judgement about evidence and its age, not a copy of the
column:

* **green** -- the account answered, recently enough to believe.
* **red** -- the last thing we learned was a refusal.
* **amber** -- it said `OK`, but nothing has confirmed that lately. Not an
  error and not a reassurance; "nobody has checked" is its own answer and
  deserves its own colour rather than being rounded to green.

`title` carries the age, so hovering a light says how old the claim is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SourceLight", "source_lights"]

#: Older than this and a green becomes amber. The scheduler probes failing
#: accounts every ten minutes and scans working ones more often than that, so
#: half an hour without a word is genuinely unusual rather than merely quiet.
STALE_AFTER_MINUTES = 30


@dataclass(slots=True)
class SourceLight:
    account: str
    brand_name: str
    state: str          # ok | err | warn
    title: str          # the age and the reason, for a tooltip

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _age(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    if minutes < 1440:
        return f"{int(minutes // 60)} h ago"
    return f"{int(minutes // 1440)} days ago"


async def source_lights(session: AsyncSession) -> list[SourceLight]:
    """One light per enabled account, in the current RLS scope."""
    rows = (
        await session.execute(
            text(
                """
                SELECT c.name, b.name AS brand_name, c.status::text AS status,
                       extract(epoch from now() - c.last_probe_at) / 60 AS probe_min,
                       (SELECT extract(epoch from now() - max(r.started_at)) / 60
                          FROM sync_runs r
                         WHERE r.connection_id = c.id AND r.ok) AS ok_min
                FROM commpeak_connections c
                JOIN brands b ON b.id = c.brand_id
                WHERE c.is_enabled
                ORDER BY b.name, c.name
                """
            )
        )
    ).mappings().all()

    lights: list[SourceLight] = []
    for row in rows:
        probe = float(row["probe_min"]) if row["probe_min"] is not None else None
        ok_run = float(row["ok_min"]) if row["ok_min"] is not None else None
        # The freshest thing that actually succeeded, whichever it was.
        freshest = min([m for m in (probe, ok_run) if m is not None], default=None)

        if row["status"] == "ERROR":
            state = "err"
            title = f"refused — last checked {_age(probe)}"
        elif freshest is None:
            state = "warn"
            title = "never confirmed"
        elif freshest > STALE_AFTER_MINUTES:
            # Deliberately not green. The column says OK; nothing has agreed
            # with it lately, and that distinction is the whole point.
            state = "warn"
            title = f"said OK, but nothing has confirmed it since {_age(freshest)}"
        else:
            state = "ok"
            title = f"answering — confirmed {_age(freshest)}"

        lights.append(
            SourceLight(
                account=row["name"],
                brand_name=row["brand_name"],
                state=state,
                title=title,
            )
        )
    return lights


def as_context(lights: list[SourceLight]) -> dict[str, Any]:
    return {
        "lights": [
            {"account": light.account, "brand_name": light.brand_name,
             "state": light.state, "title": light.title}
            for light in lights
        ],
        "all_ok": all(light.ok for light in lights) if lights else False,
        "bad": sum(1 for light in lights if light.state == "err"),
    }
