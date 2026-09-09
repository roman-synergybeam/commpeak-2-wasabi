"""CommPeak CDR client, against the documented PBX Stats API.

The contract, from https://docs.commpeak.com/reference/searchcdrs:

    POST https://<instance>.stats.pbx.commpeak.com/api/cdrs
    Content-Type: application/x-www-form-urlencoded
    Authorization: <API key>

    page, cdrs_per_page        pagination, 1-based
    from, till                 the range; defaults are yesterday 00:00 and now
    sort_by, sort_direction    ordering
    country                    ISO-3166 alpha-2, e.g. IL,UA,US
    direction, call_type, destination, source, extension, did, caller_id,
    hangup_cause, agent, queue, bridged_agent, uniqueid, id, successful,
    completed, transferred, shift            filters
    {custom_field}             any custom field, by name

    -> {"cdrs": [ ... ]}

Three things in here were guesses before the reference was found, and all three
were wrong: it was a GET with query parameters, paged with limit/offset, and
read a date range called ``start_at_from``. It is a form-encoded POST, paged
with ``page``/``cdrs_per_page``, and the range is ``from``/``till``.

The response carries what the calls page needs and this system was previously
inferring or missing: ``country_name`` for the destination country,
``agent_name`` and ``agent_pbxExtension`` for who handled it, ``queue_name``,
``bill_duration``, ``waiting_time``, ``cost`` and ``recording_link``.

Timestamps come back without a zone. They are read as UTC, which is consistent
with the recording filenames: the documented example key's channel id decodes
to exactly its wall-clock field in UTC.
"""

from __future__ import annotations

import enum
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.commpeak.correlate import normalise_msisdn
from c2w.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "AuthScheme",
    "CdrApiConfig",
    "CdrClient",
    "CdrQueryFilters",
    "ingest_page",
    "normalise_cdr",
    "parse_cdr_timestamp",
    "poll_window",
]

_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
DEFAULT_PAGE_SIZE = 500
#: CommPeak's timestamps carry no zone; the recordings confirm they are UTC.
_TIMESTAMP_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z")


class AuthScheme(enum.StrEnum):
    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"
    QUERY = "query"


@dataclass(slots=True)
class CdrApiConfig:
    """How to reach one CommPeak PBX Stats instance.

    ``base_url`` is the instance's own host -- the API is per-instance, not a
    shared endpoint, which is why every account carries its own.
    """

    base_url: str
    path: str = "/api/cdrs"
    auth: AuthScheme = AuthScheme.HEADER
    token: str = ""
    username: str = ""
    header_name: str = "Authorization"
    #: The API's own name for the page size.
    page_size: int = 500
    sort_by: str = "call_start"
    sort_direction: str = "desc"
    response_format: str = "json"
    verify_tls: bool = True


@dataclass(slots=True)
class CdrQueryFilters:
    """The documented filters, as a narrowing on a fetch.

    Only the ones with a value are sent, so an empty object fetches everything
    in the range.
    """

    country: str = ""          # ISO-3166 alpha-2, comma separated
    direction: str = ""
    call_type: str = ""
    destination: str = ""
    source: str = ""
    extension: str = ""
    did: str = ""
    caller_id: str = ""
    hangup_cause: str = ""
    agent: str = ""
    queue: str = ""
    uniqueid: str = ""
    successful: bool | None = None
    completed: bool | None = None
    transferred: bool | None = None
    bill_duration_from: int | None = None
    #: Any custom field the instance defines, by its own name.
    custom: dict[str, str] = field(default_factory=dict)

    def as_form(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in (
            "country", "direction", "call_type", "destination", "source",
            "extension", "did", "caller_id", "hangup_cause", "agent", "queue",
            "uniqueid",
        ):
            if value := getattr(self, name):
                out[name] = str(value)
        for name in ("successful", "completed", "transferred"):
            value = getattr(self, name)
            if value is not None:
                out[name] = "1" if value else "0"
        if self.bill_duration_from is not None:
            out["bill_duration_from"] = str(self.bill_duration_from)
        out.update({k: str(v) for k, v in self.custom.items() if v})
        return out


def parse_cdr_timestamp(value: Any) -> datetime | None:
    """Parse a CDR timestamp, assuming UTC when no zone is given."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    raw = str(value).strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    log.debug("cdr.unparseable_timestamp", value=raw[:64])
    return None


def _direction_of(record: dict[str, Any]) -> str | None:
    """Map the API's ``type`` onto inbound or outbound.

    The value is free-form across instances, so the check is on what it
    contains rather than on an exact match.
    """
    raw = str(record.get("type") or record.get("direction") or "").strip().lower()
    if not raw:
        # The other CDR source has no direction field, but an @did. source is
        # an inbound DID leg -- the same signal the recording filenames encode.
        return "in" if "@did." in str(record.get("src") or "") else None
    if "out" in raw:
        return "out"
    if "in" in raw:
        return "in"
    if raw in ("internal", "local"):
        return "internal"
    return None


def _seconds(value: Any) -> int | None:
    """Durations arrive as strings, and sometimes as H:MM:SS."""
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if 1 < len(parts) <= 3 and all(part.strip().isdigit() for part in parts):
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    try:
        return int(float(raw))
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    """Cost as an exact number, or None.

    ``Decimal`` rather than ``float``: this is money, it is summed for
    reporting, and CommPeak sends it as a string. Anything unparseable becomes
    None rather than zero -- a missing cost and a free call are different
    facts, and conflating them quietly understates a bill.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return None


def normalise_cdr(record: dict[str, Any]) -> dict[str, Any]:
    """Map one documented CDR onto our columns.

    Field names come from the PBX Stats reference, with the names from the
    other CDR source accepted as fallbacks -- the two do not agree, and an
    instance may be on either.

    The whole payload is kept in ``raw`` so a field not modelled yet -- custom
    fields, cost, desks -- is never lost, and a later column can be backfilled
    from what was already stored.

    ``src_norm``/``dst_norm`` are computed on write rather than at query time:
    number search and recording correlation both compare digit suffixes, and
    doing that per row on read would make both slow.
    """
    start_at = parse_cdr_timestamp(record.get("call_start") or record.get("start_at"))
    end_at = parse_cdr_timestamp(record.get("call_end") or record.get("end_at"))
    source = record.get("caller_id") or record.get("source") or record.get("src")
    destination = record.get("destination") or record.get("dst")

    # PBX Stats calls it uniqueid; the other source called it call_uuid.
    call_uuid = record.get("uniqueid") or record.get("call_uuid") or record.get("id")
    numeric_id = record.get("id") if str(record.get("id") or "").isdigit() else None

    return {
        "call_uuid": str(call_uuid) if call_uuid is not None else None,
        "call_id": int(numeric_id) if numeric_id is not None else record.get("call_id"),
        "start_at": start_at,
        "end_at": end_at,
        "call_duration": _seconds(record.get("duration") or record.get("call_duration")),
        "direction": _direction_of(record),
        "src": source,
        "dst": destination,
        "src_norm": normalise_msisdn(source) or None,
        "dst_norm": normalise_msisdn(destination) or None,
        # The destination country, which the calls page shows as a column.
        "dst_country": record.get("country_name") or record.get("dst_country"),
        # Whoever handled it. A transferred call has a second agent, and
        # "who dealt with this" then has two answers -- keep both.
        "agent_extension": record.get("agent_pbxExtension")
        or record.get("agent_callerid_number"),
        "agent_name": record.get("agent_name") or record.get("agent_callerid_name"),
        "bridged_agent_name": record.get("bridged_agent_name"),
        "bridged_agent_extension": record.get("bridged_agent_pbxExtension"),
        # The route: what kind of call, and which queue it came through. There
        # is no carrier or trunk field in the CDR -- the nearest thing to a
        # provider is the CommPeak account it arrived on, which is the
        # connection this row already belongs to.
        "call_type": record.get("type") or record.get("call_type"),
        "queue_name": record.get("queue_name") or record.get("queue_alias"),
        "bill_duration": _seconds(record.get("bill_duration")),
        "cost": _decimal(record.get("cost")),
        "caller_user": record.get("caller_user") or record.get("caller_username"),
        "client_callerid_name": record.get("source_name")
        or record.get("client_callerid_name"),
        "client_callerid_number": record.get("client_callerid_number"),
        "status": record.get("hangup_cause") or record.get("status"),
        "hangup_disposition": record.get("hangup_disposition")
        or record.get("client_hangup_disposition")
        or record.get("agent_hangup_disposition"),
        "public_recording_url": record.get("recording_link")
        or record.get("public_recording_url")
        or record.get("record_file"),
        "raw": record,
    }


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    """Pull the record list out of whatever envelope the API used."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "cdrs", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # A single record returned bare.
        if "call_uuid" in payload:
            return [payload]
    return []


class CdrClient:
    """Reads CDRs from one PBX Stats instance."""

    def __init__(self, config: CdrApiConfig) -> None:
        self.config = config

    def _auth(self) -> tuple[dict[str, str], dict[str, str], httpx.Auth | None]:
        headers = {"Accept": "application/json"}
        form: dict[str, str] = {}
        auth: httpx.Auth | None = None
        match self.config.auth:
            case AuthScheme.HEADER:
                headers[self.config.header_name] = self.config.token
            case AuthScheme.BEARER:
                headers["Authorization"] = f"Bearer {self.config.token}"
            case AuthScheme.BASIC:
                auth = httpx.BasicAuth(self.config.username, self.config.token)
            case AuthScheme.QUERY:
                form["api_key"] = self.config.token
            case AuthScheme.NONE:
                pass
        return headers, form, auth

    async def fetch_range(
        self,
        start: datetime,
        end: datetime,
        *,
        filters: CdrQueryFilters | None = None,
        max_pages: int = 500,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield pages of CDRs covering ``[start, end]``.

        A form-encoded POST, because that is what the API takes -- not a GET
        with query parameters. Pages are 1-based via ``page``/``cdrs_per_page``.

        ``max_pages`` bounds a run: an instance that ignores the page parameter
        would otherwise return page one for ever.
        """
        headers, base_form, auth = self._auth()
        url = self.config.base_url.rstrip("/") + "/" + self.config.path.lstrip("/")
        narrowing = (filters or CdrQueryFilters()).as_form()

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=self.config.verify_tls, auth=auth
        ) as client:
            for page in range(1, max_pages + 1):
                form = {
                    **base_form,
                    **narrowing,
                    "format": self.config.response_format,
                    "page": str(page),
                    "cdrs_per_page": str(self.config.page_size),
                    "sort_by": self.config.sort_by,
                    "sort_direction": self.config.sort_direction,
                    "from": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "till": end.strftime("%Y-%m-%d %H:%M:%S"),
                }
                response = await client.post(url, data=form, headers=headers)
                if response.status_code == 401:
                    raise PermissionError("the CDR API rejected the credentials (401)")
                if response.status_code == 403:
                    raise PermissionError(
                        "the CDR API returned 403; check the API key's permissions "
                        "and whether this server's address is allowed"
                    )
                response.raise_for_status()

                records = _extract_records(response.json())
                if not records:
                    return
                yield records
                if len(records) < self.config.page_size:
                    return
            log.warning(
                "cdr.page_limit_reached",
                max_pages=max_pages,
                detail="stopping; check that the page parameter is honoured",
            )


async def ingest_page(
    session: AsyncSession,
    *,
    brand_id: int,
    connection_id: int,
    tenant_id: int,
    records: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    """Upsert a page of CDRs.  Returns ``(inserted, updated)``.

    Upsert rather than insert because a call's CDR is written when the call
    starts and updated when it ends: re-polling an overlapping window is normal
    and must refresh the row, not duplicate it or fail.
    """
    inserted = updated = 0
    for record in records:
        row = normalise_cdr(record)
        if not row["call_uuid"] or row["start_at"] is None:
            # Without a call_uuid there is nothing to correlate against, and
            # without a start time it cannot be matched to a recording at all.
            continue
        result = await session.execute(
            text(
                """
                INSERT INTO cdrs (
                    brand_id, connection_id, tenant_id, call_uuid, call_id,
                    start_at, end_at, call_duration, direction, src, dst,
                    src_norm, dst_norm, dst_country, agent_extension, agent_name,
                    bridged_agent_name, bridged_agent_extension,
                    call_type, queue_name, bill_duration, cost,
                    caller_user, client_callerid_name, client_callerid_number,
                    status, hangup_disposition, public_recording_url, raw
                ) VALUES (
                    :brand_id, :connection_id, :tenant_id, :call_uuid, :call_id,
                    :start_at, :end_at, :call_duration, :direction, :src, :dst,
                    :src_norm, :dst_norm, :dst_country, :agent_extension, :agent_name,
                    :bridged_agent_name, :bridged_agent_extension,
                    :call_type, :queue_name, :bill_duration, :cost,
                    :caller_user, :client_callerid_name, :client_callerid_number,
                    :status, :hangup_disposition, :public_recording_url, :raw
                )
                -- "Was this row new?" is normally answered with
                -- RETURNING (xmax = 0), but xmax is a system column and
                -- asyncpg refuses to read one from an INSERT routed through a
                -- partitioned parent: "cannot retrieve a system column in this
                -- context". cdrs is partitioned by brand, so that form failed
                -- for every row -- ingest could not have worked at all. The
                -- timestamps answer the same question without a system column.
                ON CONFLICT (brand_id, connection_id, call_uuid) DO UPDATE SET
                    end_at = COALESCE(EXCLUDED.end_at, cdrs.end_at),
                    call_duration = COALESCE(EXCLUDED.call_duration, cdrs.call_duration),
                    status = COALESCE(EXCLUDED.status, cdrs.status),
                    -- A re-read of the same call can fill in what was not
                    -- known the first time: a transfer's second agent, the
                    -- billed duration and the cost all settle after hangup.
                    bridged_agent_name = COALESCE(
                        EXCLUDED.bridged_agent_name, cdrs.bridged_agent_name
                    ),
                    bridged_agent_extension = COALESCE(
                        EXCLUDED.bridged_agent_extension, cdrs.bridged_agent_extension
                    ),
                    call_type = COALESCE(EXCLUDED.call_type, cdrs.call_type),
                    queue_name = COALESCE(EXCLUDED.queue_name, cdrs.queue_name),
                    bill_duration = COALESCE(EXCLUDED.bill_duration, cdrs.bill_duration),
                    cost = COALESCE(EXCLUDED.cost, cdrs.cost),
                    hangup_disposition = COALESCE(
                        EXCLUDED.hangup_disposition, cdrs.hangup_disposition
                    ),
                    public_recording_url = COALESCE(
                        EXCLUDED.public_recording_url, cdrs.public_recording_url
                    ),
                    raw = EXCLUDED.raw,
                    -- clock_timestamp(), not now(): now() is fixed for the
                    -- whole transaction, so an insert and an update in the
                    -- same batch would be indistinguishable. clock_timestamp()
                    -- always advances, which makes the test below exact.
                    updated_at = clock_timestamp()
                RETURNING (created_at = updated_at) AS was_inserted
                """
            ),
            {
                "brand_id": brand_id,
                "connection_id": connection_id,
                "tenant_id": tenant_id,
                **row,
                "raw": json.dumps(row["raw"], default=str),
            },
        )
        was_inserted = result.scalar_one()
        if was_inserted:
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def poll_window(
    last_cursor: datetime | None, *, now: datetime | None = None, overlap_minutes: int = 15
) -> tuple[datetime, datetime]:
    """Pick the time range for the next CDR poll.

    Overlaps the previous window because a CDR is finalised when the call ends:
    a long call that started inside the last window may only have appeared, or
    changed, after we read it. Without the overlap those updates are missed.
    """
    now = now or datetime.now(UTC)
    if last_cursor is None:
        return now - timedelta(hours=24), now
    return last_cursor - timedelta(minutes=overlap_minutes), now
