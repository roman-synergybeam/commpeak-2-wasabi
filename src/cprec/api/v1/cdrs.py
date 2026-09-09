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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cprec.commpeak.correlate import normalise_msisdn

__all__ = ["CdrPage", "CdrQuery", "SortField", "dashboard_stats", "get_call", "search_cdrs"]

MAX_PAGE_SIZE = 200
#: Beyond this, offset paging is too slow to be useful; the UI narrows the
#: filters instead of walking further.
MAX_OFFSET = 50_000
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


@dataclass(slots=True)
class CdrPage:
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int | None = None
    limit: int = 50
    offset: int = 0
    truncated: bool = False

    @property
    def has_more(self) -> bool:
        return len(self.rows) == self.limit


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


async def search_cdrs(session: AsyncSession, query: CdrQuery) -> CdrPage:
    """Run a CDR search, with each call's recordings aggregated in."""
    limit = max(1, min(query.limit, MAX_PAGE_SIZE))
    offset = max(0, min(query.offset, MAX_OFFSET))
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    clauses = _build_filters(query, params)
    direction = "DESC" if query.descending else "ASC"

    sql = f"""
        SELECT
            c.id, c.call_uuid, c.call_id, c.start_at, c.end_at, c.call_duration,
            c.direction, c.src, c.dst, c.status, c.agent_extension, c.agent_name,
            c.caller_user, c.connection_id, c.public_recording_url,
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


async def count_cdrs(session: AsyncSession, query: CdrQuery, *, cap: int = 10_000) -> int:
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


async def dashboard_stats(session: AsyncSession) -> dict[str, Any]:
    """Headline numbers for the dashboard.

    Deliberately a handful of aggregate queries rather than one big one: they
    each hit a different index, and a single query joining all of it would be
    slower and far harder to reason about when it regresses.
    """
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
    }
