"""CDR and recording search.

The query here is the one that has to stay fast at tens of millions of rows, so
it is built around the indexes rather than around convenience:

* results are always bounded by a time range, which lets the ``(brand_id,
  start_at DESC)`` index do the work;
* number search uses the pre-normalised ``src_norm``/``dst_norm`` columns for
  exact matches and trigram indexes for partial ones, never ``LIKE '%x%'`` on
  the raw column;
* paging is by offset with a hard ceiling, because deep offsets on a 12M-row
  partition are slow enough to look broken.

Recordings are joined in rather than queried separately, and a call with no
correlated recording still appears -- a missing join must never hide a call.
"""

from __future__ import annotations

import enum
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.commpeak.correlate import normalise_msisdn

__all__ = [
    "COUNT_CAP",
    "MAX_MERGE_ROWS",
    "MAX_OFFSET",
    "MAX_PAGE_SIZE",
    "CdrPage",
    "CdrQuery",
    "SortField",
    "dashboard_stats",
    "filter_options",
    "forget_cached_stats",
    "get_call",
    "search_cdrs",
]

MAX_PAGE_SIZE = 200
#: Beyond this, offset paging is too slow to be useful; the UI narrows the
#: filters instead of walking further.
MAX_OFFSET = 50_000
#: How many rows a *merge* may pull from one organisation.
#:
#: Reading a page across several organisations means asking each for
#: ``offset + limit`` rows and slicing the merged order (see `_search_across`),
#: which needs more rows than a client is ever handed. `MAX_PAGE_SIZE` is a cap
#: on a response, not on an internal read, and using it for both silently
#: dropped rows from page three onwards. At 100 rows a page this is exact
#: through page fifty; past that the page is marked truncated rather than
#: quietly wrong.
MAX_MERGE_ROWS = 5_000
#: Where the match count stops being exact.
#:
#: `count_cdrs` counts to here and no further, so a total at this value means
#: "at least this many". That is why the page list stops offering a jump to the
#: last page once it is reached -- there is no known last page, and a numbered
#: link to one would be a guess presented as a fact.
COUNT_CAP = 10_000
DEFAULT_RANGE_DAYS = 7


class SortField(enum.StrEnum):
    STARTED = "started"
    DURATION = "duration"
    SRC = "src"
    DST = "dst"

    @property
    def column(self) -> str:
        return {
            "started": "c.start_at",
            "duration": "c.call_duration",
            "src": "c.src",
            "dst": "c.dst",
        }[self.value]


@dataclass(slots=True)
class CdrQuery:
    date_from: datetime | None = None
    date_to: datetime | None = None
    number: str | None = None
    direction: str | None = None
    agent: str | None = None
    status: str | None = None
    #: Destination country, matched on the name CommPeak sends.
    country: str | None = None
    queue: str | None = None
    call_type: str | None = None
    call_uuid: str | None = None
    connection_id: int | None = None
    #: Filter by archive state: "available", "pending", "failed", "orphan".
    media: str | None = None
    min_duration: int | None = None
    sort: SortField = SortField.STARTED
    descending: bool = True
    limit: int = 50
    offset: int = 0

    def effective_range(self) -> tuple[datetime, datetime]:
        """Always bound the range, so the time index is always usable."""
        end = self.date_to or datetime.now(UTC)
        start = self.date_from or (end - timedelta(days=DEFAULT_RANGE_DAYS))
        return start, end


def _with_gaps(pages: list[int], *, open_ended: bool) -> list[int | None]:
    """Insert a None wherever the sequence skips a page."""
    out: list[int | None] = []
    previous: int | None = None
    for number in pages:
        if previous is not None and number > previous + 1:
            out.append(None)
        out.append(number)
        previous = number
    if open_ended:
        # The list ends open, so a trailing gap says "there is more" without
        # claiming a number for it.
        out.append(None)
    return out


@dataclass(slots=True)
class CdrPage:
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    limit: int = 50
    offset: int = 0
    truncated: bool = False
    #: True when `total` is a floor rather than a count -- see `COUNT_CAP`.
    count_capped: bool = False

    @property
    def has_more(self) -> bool:
        return len(self.rows) == self.limit

    @property
    def page_number(self) -> int:
        """Which page this is, counting from 1."""
        return self.offset // max(1, self.limit) + 1

    @property
    def page_count(self) -> int | None:
        """How many pages there are, or None when that is not known.

        None is the honest answer whenever the total is capped: the operator can
        keep going, but nobody knows how far, and a page count derived from a
        floor would read as the end of the list.
        """
        if self.total is None or self.count_capped:
            return None
        return max(1, math.ceil(self.total / max(1, self.limit)))

    def page_links(self, span: int = 2) -> list[int | None]:
        """The numbers to render, with None standing for a gap.

        Two hundred numbered links is not navigation, so the list is the first
        page, the last page, and `span` either side of the current one, with a
        gap marker where numbers were left out. Built here rather than in the
        template because it is the arithmetic that goes wrong at the edges --
        page one, the last page, a total shorter than the window -- and those
        cases are worth testing.
        """
        here = self.page_number
        last = self.page_count
        if last is None:
            # Unknown length: offer the neighbourhood behind, and Next carries
            # the operator forward.
            wanted = {1, *range(max(1, here - span), here + 1)}
            return _with_gaps(sorted(wanted), open_ended=True)
        wanted = {1, last, *range(max(1, here - span), min(last, here + span) + 1)}
        return _with_gaps(sorted(wanted), open_ended=False)


def _build_filters(query: CdrQuery, params: dict[str, Any]) -> list[str]:
    start, end = query.effective_range()
    params["start"] = start
    params["end"] = end
    clauses = ["c.start_at >= :start", "c.start_at <= :end"]

    if query.connection_id:
        clauses.append("c.connection_id = :connection_id")
        params["connection_id"] = query.connection_id

    if query.call_uuid:
        clauses.append("c.call_uuid = :call_uuid")
        params["call_uuid"] = query.call_uuid.strip()

    if query.number:
        raw = query.number.strip()
        digits = normalise_msisdn(raw)
        if digits and len(digits) >= 6:
            # Exact suffix match on the normalised columns hits a btree index.
            clauses.append("(c.src_norm = :digits OR c.dst_norm = :digits)")
            params["digits"] = digits
        else:
            # Partial search: the trigram indexes make this viable.
            clauses.append("(c.src ILIKE :like OR c.dst ILIKE :like)")
            params["like"] = f"%{raw}%"

    if query.direction in ("in", "out"):
        clauses.append("c.direction = :direction")
        params["direction"] = query.direction

    if query.agent:
        clauses.append("(c.agent_extension = :agent OR c.agent_name ILIKE :agent_like)")
        params["agent"] = query.agent.strip()
        params["agent_like"] = f"%{query.agent.strip()}%"

    if query.status:
        clauses.append("c.status = :status")
        params["status"] = query.status

    if query.country:
        # Exact on the indexed column: the values come from a menu built out of
        # what is actually in this organisation's data, so there is nothing to
        # match loosely against.
        clauses.append("c.dst_country = :country")
        params["country"] = query.country.strip()

    if query.queue:
        clauses.append("c.queue_name = :queue")
        params["queue"] = query.queue.strip()

    if query.call_type:
        clauses.append("c.call_type = :call_type")
        params["call_type"] = query.call_type.strip()

    if query.min_duration:
        clauses.append("c.call_duration >= :min_duration")
        params["min_duration"] = query.min_duration

    # Correlated on (cdr_id, brand_id) so the subquery stays inside the call's
    # own partition rather than probing every brand's recordings.
    _linked = "SELECT 1 FROM recordings r WHERE r.cdr_id = c.id AND r.brand_id = c.brand_id"
    _archived = "('AVAILABLE','SOURCE_DELETED')"
    match query.media:
        case "available":
            clauses.append(f"EXISTS ({_linked} AND r.state IN {_archived})")
        case "pending":
            clauses.append(
                f"EXISTS ({_linked} AND r.state NOT IN ('AVAILABLE','SOURCE_DELETED','FAILED'))"
            )
        case "failed":
            clauses.append(f"EXISTS ({_linked} AND r.state IN ('FAILED','MISSING_SOURCE'))")
        case "none":
            clauses.append(f"NOT EXISTS ({_linked})")
    return clauses


async def search_cdrs(
    session: AsyncSession, query: CdrQuery, *, cap: int = MAX_PAGE_SIZE
) -> CdrPage:
    """Run a CDR search, with each call's recordings aggregated in.

    `cap` is the ceiling on how many rows may come back. It defaults to
    `MAX_PAGE_SIZE`, which is what any caller answering a client should use, so
    the public API's guard is exactly what it was. Only the cross-organisation
    merge raises it, because it has to over-read each organisation to slice a
    merged page and is not handing those rows to anybody.
    """
    limit = max(1, min(query.limit, cap))
    offset = max(0, min(query.offset, MAX_OFFSET))
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    clauses = _build_filters(query, params)
    direction = "DESC" if query.descending else "ASC"

    sql = f"""
        SELECT
            c.id, c.call_uuid, c.call_id, c.start_at, c.end_at, c.call_duration,
            c.direction, c.src, c.dst, c.status, c.agent_extension, c.agent_name,
            c.caller_user, c.connection_id, c.public_recording_url,
            c.dst_country, c.call_type, c.queue_name,
            c.bridged_agent_name, c.bridged_agent_extension,
            c.bill_duration, c.cost,
            -- The nearest thing to a "SIP provider" that exists: CommPeak's
            -- CDR carries no carrier and no trunk field, but each CommPeak
            -- account is one PBX or dialer with its own trunk, and every call
            -- already knows which account it arrived on.
            conn.name                   AS connection_name,
            COALESCE(m.parts, 0)        AS recording_parts,
            m.states                    AS recording_states,
            m.first_recording_id        AS recording_id,
            COALESCE(m.bytes, 0)        AS recording_bytes,
            m.match_method              AS match_method,
            m.match_confidence          AS match_confidence
        FROM cdrs c
        LEFT JOIN (
            SELECT
                r.cdr_id,
                r.brand_id,
                count(*)                        AS parts,
                array_agg(DISTINCT r.state::text) AS states,
                min(r.id)                       AS first_recording_id,
                sum(r.source_size)              AS bytes,
                min(r.match_method)             AS match_method,
                max(r.match_confidence)         AS match_confidence
            FROM recordings r
            WHERE r.cdr_id IS NOT NULL
            GROUP BY r.cdr_id, r.brand_id
        ) m ON m.cdr_id = c.id AND m.brand_id = c.brand_id
        LEFT JOIN commpeak_connections conn
               ON conn.id = c.connection_id AND conn.brand_id = c.brand_id
        WHERE {" AND ".join(clauses)}
        ORDER BY {query.sort.column} {direction} NULLS LAST, c.id {direction}
        LIMIT :limit OFFSET :offset
    """  # noqa: S608 - clauses are fixed literals; every value is a bound parameter

    rows = (await session.execute(text(sql), params)).mappings().all()
    return CdrPage(
        rows=[dict(r) for r in rows],
        limit=limit,
        offset=offset,
        truncated=offset >= MAX_OFFSET,
    )


async def filter_options(session: AsyncSession, *, days: int = 90) -> dict[str, list[str]]:
    """The values to offer in the country, queue and call-type menus.

    Read from the data rather than hardcoded, because the answer is different
    for each organisation and changes as they open new destinations -- a fixed
    country list would be wrong the first time somebody dials somewhere new.

    Bounded by a recent window and by RLS, so this is one index scan inside the
    brand's own partition rather than a walk over every call ever made. An
    empty list is a fine answer: it means the menu is not worth showing yet.
    """
    sql = """
        SELECT
            array_agg(DISTINCT dst_country) FILTER (WHERE dst_country IS NOT NULL) AS countries,
            array_agg(DISTINCT queue_name)  FILTER (WHERE queue_name  IS NOT NULL) AS queues,
            array_agg(DISTINCT call_type)   FILTER (WHERE call_type   IS NOT NULL) AS call_types
        FROM cdrs
        WHERE start_at >= now() - make_interval(days => :days)
    """
    row = (await session.execute(text(sql), {"days": days})).mappings().one()
    return {
        "countries": sorted(row["countries"] or []),
        "queues": sorted(row["queues"] or []),
        "call_types": sorted(row["call_types"] or []),
    }


async def count_cdrs(session: AsyncSession, query: CdrQuery, *, cap: int = COUNT_CAP) -> int:
    """Count matches, stopping at ``cap``.

    An exact count over a multi-million-row range costs as much as the search
    itself and nobody reads past "10,000+", so the count is bounded and the UI
    says "10,000+" rather than pretending to precision it paid dearly for.
    """
    params: dict[str, Any] = {"cap": cap}
    clauses = _build_filters(query, params)
    sql = f"""
        SELECT count(*) FROM (
            SELECT 1 FROM cdrs c WHERE {" AND ".join(clauses)} LIMIT :cap
        ) capped
    """  # noqa: S608 - see search_cdrs
    return (await session.execute(text(sql), params)).scalar_one()


async def get_call(session: AsyncSession, cdr_id: int) -> dict[str, Any] | None:
    """One call with every recording part attached, ordered as recorded."""
    call = (
        await session.execute(
            text(
                """
                SELECT c.*, t.name AS tenant_name, k.name AS connection_name
                FROM cdrs c
                LEFT JOIN tenants t ON t.id = c.tenant_id
                LEFT JOIN commpeak_connections k ON k.id = c.connection_id
                WHERE c.id = :id
                """
            ),
            {"id": cdr_id},
        )
    ).mappings().first()
    if call is None:
        return None

    parts = (
        (
            await session.execute(
                text(
                    """
                    SELECT id, seq, state::text AS state, source_key, source_size,
                           destination_key, checksum_sha256, verified_at, direction,
                           number, extension, started_at, file_ext, match_method,
                           match_confidence, match_ambiguous, last_error_class,
                           last_error_detail, bytes_transferred
                    FROM recordings
                    WHERE cdr_id = :id
                    ORDER BY seq, id
                    """
                ),
                {"id": cdr_id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(call), "recordings": [dict(p) for p in parts]}


async def get_recording(session: AsyncSession, recording_id: int) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM recordings WHERE id = :id"), {"id": recording_id}
        )
    ).mappings().first()
    return dict(row) if row else None


#: Counts that cost a full table scan, remembered for a few seconds.
#:
#: Three of the queries below cannot avoid reading every row: counting four
#: million recordings by state, summing their sizes, and counting nine million
#: queue rows by state. There is no index that helps -- an index-only scan was
#: measured and the planner is right to prefer the sequential one -- so the
#: only lever is how often they run.
#:
#: They ran on **every** dashboard refresh, which is every ten seconds, once
#: per organisation. Measured: 11.6 GB of buffer reads per organisation per
#: refresh, 23 GB every ten seconds for two, which was three of this host's
#: eight cores doing nothing but recounting numbers that had barely moved.
#:
#: Keyed by the RLS scope rather than by a brand argument, because the scope is
#: what actually determines the answer -- so one organisation's counts can
#: never be served to another even if a caller forgets to pass the brand.
#: Process-local on purpose: it is a cache, not state, and a restart losing it
#: costs one recount.
_STATS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

#: Cleared by the tests, and by anything that needs a recount now.
def forget_cached_stats() -> None:
    _STATS_CACHE.clear()


async def _scan_counts(session: AsyncSession, cache_seconds: int) -> dict[str, Any]:
    """The three full-table aggregates, cached for `cache_seconds`."""
    scope = (
        await session.execute(
            text("SELECT coalesce(current_setting('c2w.brand_id', true), '')")
        )
    ).scalar_one() or "unscoped"

    now = time.monotonic()
    if cache_seconds > 0:
        cached = _STATS_CACHE.get(scope)
        if cached is not None and cached[0] > now:
            return cached[1]

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    recording_states = dict(
        (
            await session.execute(
                text("SELECT state::text, count(*) FROM recordings GROUP BY state")
            )
        ).all()
    )
    job_states = dict(
        (
            await session.execute(
                text("SELECT state::text, count(*) FROM transfer_jobs GROUP BY state")
            )
        ).all()
    )
    totals = (
        await session.execute(
            text(
                """
                SELECT
                    count(*)                                       AS recordings,
                    COALESCE(sum(source_size), 0)                  AS bytes_source,
                    COALESCE(sum(bytes_transferred), 0)            AS bytes_archived,
                    count(*) FILTER (WHERE match_method = 'orphan') AS orphans,
                    count(*) FILTER (WHERE started_at >= :today)   AS today
                FROM recordings
                """
            ),
            {"today": today},
        )
    ).mappings().one()

    counts = {
        "recording_states": recording_states,
        "job_states": job_states,
        "totals": dict(totals),
        "counted_at": datetime.now(UTC),
    }
    if cache_seconds > 0:
        _STATS_CACHE[scope] = (now + cache_seconds, counts)
    return counts


async def dashboard_stats(
    session: AsyncSession, *, cache_seconds: int = 0
) -> dict[str, Any]:
    """Headline numbers for the dashboard.

    Deliberately a handful of aggregate queries rather than one big one: they
    each hit a different index, and a single query joining all of it would be
    slower and far harder to reason about when it regresses.

    `cache_seconds` applies only to the three that read whole tables (see
    `_scan_counts`). The genuinely live parts -- what scanned recently, how
    many calls today -- are always fresh, because those are the ones somebody
    watching the page is actually watching. Defaults to 0 so a caller that has
    not thought about it gets exact numbers rather than silently stale ones.
    """
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    counts = await _scan_counts(session, cache_seconds)
    recording_states = counts["recording_states"]
    job_states = counts["job_states"]
    totals = counts["totals"]

    calls_today = (
        await session.execute(
            text("SELECT count(*) FROM cdrs WHERE start_at >= :today"), {"today": today}
        )
    ).scalar_one()

    recent_runs = (
        (
            await session.execute(
                text(
                    """
                    SELECT kind::text, started_at, finished_at, ok, discovered, queued,
                           transferred, failed, error_detail, connection_id
                    FROM sync_runs
                    ORDER BY started_at DESC
                    LIMIT 10
                    """
                )
            )
        )
        .mappings()
        .all()
    )

    archived = recording_states.get("AVAILABLE", 0) + recording_states.get("SOURCE_DELETED", 0)
    return {
        "recording_states": recording_states,
        "job_states": job_states,
        "recordings_total": totals["recordings"],
        "recordings_today": totals["today"],
        "calls_today": calls_today,
        "bytes_source": totals["bytes_source"],
        "bytes_archived": totals["bytes_archived"],
        "orphans": totals["orphans"],
        "archived": archived,
        "pending": sum(
            n
            for state, n in recording_states.items()
            if state in ("DISCOVERED", "QUEUED", "TRANSFERRING", "UPLOADED", "VERIFIED")
        ),
        "failed": recording_states.get("FAILED", 0)
        + recording_states.get("MISSING_SOURCE", 0),
        "progress_pct": round(100 * archived / totals["recordings"], 1)
        if totals["recordings"]
        else 0.0,
        "recent_runs": [dict(r) for r in recent_runs],
        # So the page can say how old the totals are rather than implying they
        # are as live as everything beside them.
        "counted_at": counts["counted_at"],
    }
