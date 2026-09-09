"""Searching stored text messages.

Deliberately the same shape as the CDR search next door -- a dataclass of
filters, a page of plain dict rows, one bounded query -- so the two pages
behave the same way and neither needs its own mental model.

The one structural difference is ``occurred_at``. Sent messages have
``sent_at`` and received ones have ``received_at``, so ordering on either alone
would interleave the other direction wrongly; the column that every row has is
what the index and the sort use.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.commpeak.correlate import normalise_msisdn

__all__ = [
    "MessagePage",
    "MessageQuery",
    "MessageSort",
    "count_messages",
    "message_filter_options",
    "message_stats",
    "search_messages",
]

MAX_PAGE_SIZE = 200
MAX_OFFSET = 50_000
DEFAULT_RANGE_DAYS = 7


class MessageSort(enum.StrEnum):
    OCCURRED = "occurred"
    COST = "cost"

    @property
    def column(self) -> str:
        return {"occurred": "m.occurred_at", "cost": "m.cost"}[self.value]


@dataclass(slots=True)
class MessageQuery:
    date_from: datetime | None = None
    date_to: datetime | None = None
    number: str | None = None
    direction: str | None = None
    status: str | None = None
    country: str | None = None
    stream: str | None = None
    campaign: str | None = None
    #: Free text against the message body.
    body: str | None = None
    sort: MessageSort = MessageSort.OCCURRED
    descending: bool = True
    limit: int = 50
    offset: int = 0

    def effective_range(self) -> tuple[datetime, datetime]:
        """Always bounded, so the time index is always usable."""
        end = self.date_to or datetime.now(UTC)
        start = self.date_from or (end - timedelta(days=DEFAULT_RANGE_DAYS))
        return start, end


@dataclass(slots=True)
class MessagePage:
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    limit: int = 50
    offset: int = 0
    truncated: bool = False

    @property
    def has_more(self) -> bool:
        return len(self.rows) == self.limit


def _build_filters(query: MessageQuery, params: dict[str, Any]) -> list[str]:
    start, end = query.effective_range()
    params["start"] = start
    params["end"] = end
    clauses = ["m.occurred_at >= :start", "m.occurred_at <= :end"]

    if query.direction in ("in", "out"):
        clauses.append("m.direction = :direction")
        params["direction"] = query.direction

    if query.number:
        raw = query.number.strip()
        digits = normalise_msisdn(raw)
        if digits and len(digits) >= 6:
            # Suffix match on the normalised columns, which are indexed.
            clauses.append("(m.source_norm = :digits OR m.destination_norm = :digits)")
            params["digits"] = digits
        else:
            clauses.append(
                "(m.source_number ILIKE :like OR m.destination_number ILIKE :like)"
            )
            params["like"] = f"%{raw}%"

    if query.status:
        clauses.append("m.status = :status")
        params["status"] = query.status

    if query.country:
        clauses.append("m.country_name = :country")
        params["country"] = query.country.strip()

    if query.stream:
        clauses.append("m.stream = :stream")
        params["stream"] = query.stream.strip()

    if query.campaign:
        clauses.append("m.campaign = :campaign")
        params["campaign"] = query.campaign.strip()

    if query.body:
        clauses.append("m.body ILIKE :body_like")
        params["body_like"] = f"%{query.body.strip()}%"

    return clauses


async def search_messages(session: AsyncSession, query: MessageQuery) -> MessagePage:
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    offset = max(0, min(query.offset, MAX_OFFSET))
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    clauses = _build_filters(query, params)
    direction = "DESC" if query.descending else "ASC"

    sql = f"""
        SELECT
            m.id, m.message_uuid, m.direction, m.status,
            m.sent_at, m.delivered_at, m.received_at, m.occurred_at,
            m.source_number, m.source_name, m.destination_number,
            m.country_code, m.country_name, m.contact_name,
            m.body, m.message_length, m.segments, m.cost,
            m.platform, m.stream, m.campaign, m.conversation,
            conn.name AS connection_name
        FROM sms_messages m
        LEFT JOIN commpeak_connections conn
               ON conn.id = m.connection_id AND conn.brand_id = m.brand_id
        WHERE {" AND ".join(clauses)}
        ORDER BY {query.sort.column} {direction} NULLS LAST, m.id {direction}
        LIMIT :limit OFFSET :offset
    """  # noqa: S608 - clauses are fixed literals; every value is a bound parameter

    rows = (await session.execute(text(sql), params)).mappings().all()
    return MessagePage(
        rows=[dict(r) for r in rows],
        limit=limit,
        offset=offset,
        truncated=offset >= MAX_OFFSET,
    )


async def count_messages(session: AsyncSession, query: MessageQuery, *, cap: int = 10_000) -> int:
    """Count matches, stopping at ``cap``.

    Capped for the same reason the calls page caps: an exact count over
    millions of rows costs more than the page it labels, and "10,000+" answers
    the question just as well.
    """
    params: dict[str, Any] = {"cap": cap}
    clauses = _build_filters(query, params)
    sql = f"""
        SELECT count(*) FROM (
            SELECT 1 FROM sms_messages m
            WHERE {" AND ".join(clauses)}
            LIMIT :cap
        ) capped
    """  # noqa: S608 - as above
    return int((await session.execute(text(sql), params)).scalar_one())


async def message_stats(session: AsyncSession, *, days: int = 7) -> dict[str, Any]:
    """Headline figures for the messages page.

    Delivery rate is computed over messages we *sent*, since an inbound message
    has no delivery status -- including them would dilute the number with rows
    it cannot describe.
    """
    sql = """
        SELECT
            count(*)                                                    AS total,
            count(*) FILTER (WHERE direction = 'out')                   AS sent,
            count(*) FILTER (WHERE direction = 'in')                    AS received,
            count(*) FILTER (WHERE direction = 'out' AND delivered_at IS NOT NULL)
                                                                        AS delivered,
            count(*) FILTER (WHERE direction = 'out' AND status IS NOT NULL
                             AND lower(status) IN ('failed', 'rejected', 'undelivered',
                                                   'expired', 'error'))  AS failed,
            COALESCE(sum(cost), 0)                                      AS cost
        FROM sms_messages
        WHERE occurred_at >= now() - make_interval(days => :days)
    """
    row = (await session.execute(text(sql), {"days": days})).mappings().one()
    stats = dict(row)
    sent = stats["sent"] or 0
    stats["delivery_rate"] = (stats["delivered"] / sent * 100) if sent else None
    return stats


async def message_filter_options(session: AsyncSession, *, days: int = 90) -> dict[str, list[str]]:
    """Values for the status, country, stream and campaign menus.

    Read from the data because the status vocabulary is not documented: the
    reference gives one example and no enumeration, so the only honest list is
    the one this account has actually produced.
    """
    sql = """
        SELECT
            array_agg(DISTINCT status)       FILTER (WHERE status IS NOT NULL)       AS statuses,
            array_agg(DISTINCT country_name) FILTER (WHERE country_name IS NOT NULL) AS countries,
            array_agg(DISTINCT stream)       FILTER (WHERE stream IS NOT NULL)       AS streams,
            array_agg(DISTINCT campaign)     FILTER (WHERE campaign IS NOT NULL)     AS campaigns
        FROM sms_messages
        WHERE occurred_at >= now() - make_interval(days => :days)
    """
    row = (await session.execute(text(sql), {"days": days})).mappings().one()
    return {
        "statuses": sorted(row["statuses"] or []),
        "countries": sorted(row["countries"] or []),
        "streams": sorted(row["streams"] or []),
        "campaigns": sorted(row["campaigns"] or []),
    }
