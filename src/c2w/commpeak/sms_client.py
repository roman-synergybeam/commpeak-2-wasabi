"""CommPeak TextPeak: reading sent and received text messages.

Written from the reference rather than from a plausible-looking URL, because
guessing the CDR contract cost real work on this project once already. What the
reference says:

    GET https://gw.commpeak.com/textpeak/streams/messages
        outgoing. Params: type, status, streamId, phone, startDate, endDate,
        page, itemsPerPage. Items carry status, sent_at, delivered_at, cost,
        platform, campaign, content.body.

    GET https://gw.commpeak.com/textpeak/streams/incoming_messages
        incoming. Params: destination, streamId, phone, startDate, endDate,
        page, itemsPerPage. Items carry received_at, from, to, contact_name,
        message_length, body -- and no status or cost, because an arrived
        message has neither.

Both authenticate with the TextPeak API key on its own in an ``Authorization``
header, and both answer ``{"items": [...], "total": n}``.

Two shapes, one table. :func:`normalise_message` maps either onto the same
columns and is the only place that knows they differ; everything downstream
sees rows.

**Delivery receipts arrive late.** A message read a minute after sending says
``sent``; the same message says ``delivered`` an hour later. So a poll that only
asked for "since last time" would leave the first answer on the record for
ever. :func:`poll_window` re-reads a configurable overlap for exactly that
reason, and the upsert lets a later read fill in what the first did not know.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.commpeak.cdr_client import parse_cdr_timestamp
from c2w.commpeak.correlate import normalise_msisdn
from c2w.logging import get_logger

__all__ = [
    "Direction",
    "SmsApiConfig",
    "SmsQueryFilters",
    "fetch_messages",
    "normalise_message",
    "poll_window",
    "store_messages",
]

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://gw.commpeak.com"
OUTGOING_PATH = "/textpeak/streams/messages"
INCOMING_PATH = "/textpeak/streams/incoming_messages"

#: A single SMS is 160 GSM-7 characters; longer messages are sent as
#: concatenated parts and billed per part. Approximate on purpose -- the exact
#: split depends on the encoding CommPeak chose, which is not in the response.
SMS_SEGMENT_CHARS = 160


class Direction(enum.StrEnum):
    OUT = "out"
    IN = "in"

    @property
    def path(self) -> str:
        return OUTGOING_PATH if self is Direction.OUT else INCOMING_PATH


@dataclass(slots=True)
class SmsApiConfig:
    """How to reach TextPeak.

    Unlike PBX Stats, the host is shared across accounts, so the default is
    usable as-is and only the key has to be configured.
    """

    token: str
    base_url: str = DEFAULT_BASE_URL
    outgoing_path: str = OUTGOING_PATH
    incoming_path: str = INCOMING_PATH
    header_name: str = "Authorization"
    page_size: int = 100
    stream_id: str = ""
    verify_tls: bool = True
    timeout_seconds: float = 30.0

    def path_for(self, direction: Direction) -> str:
        return self.outgoing_path if direction is Direction.OUT else self.incoming_path

    def headers(self) -> dict[str, str]:
        # The key on its own, with no scheme prefix. Sending "Bearer <key>"
        # here is rejected.
        return {self.header_name: self.token, "Accept": "application/json"}


@dataclass(slots=True)
class SmsQueryFilters:
    """The documented filters. Only what has a value is sent."""

    start: datetime | None = None
    end: datetime | None = None
    status: str | None = None
    phone: str | None = None
    #: Incoming only: which of our own numbers received it.
    destination: str | None = None
    stream_id: str | None = None
    message_type: str | None = None

    def as_params(self, direction: Direction) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.start:
            params["startDate"] = _stamp(self.start)
        if self.end:
            params["endDate"] = _stamp(self.end)
        if self.phone:
            params["phone"] = self.phone
        if self.stream_id:
            params["streamId"] = self.stream_id
        if direction is Direction.OUT:
            # status and type exist only on the outgoing endpoint; sending them
            # to the incoming one is at best ignored and at worst a 400.
            if self.status:
                params["status"] = self.status
            if self.message_type:
                params["type"] = self.message_type
        elif self.destination:
            params["destination"] = self.destination
        return params


def _stamp(value: datetime) -> str:
    """The format the reference's examples use: ``2026-03-19 10:15:10``, UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _decimal(value: Any) -> Decimal | None:
    """Cost as an exact number. Unparseable becomes None, never zero.

    A missing cost and a free message are different facts, and treating one as
    the other quietly understates a bill.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return None


def _numeric_suffix(value: Any) -> str | None:
    """Comparable trailing digits, but only for something that *is* a number.

    An SMS sender is very often an alphanumeric sender ID rather than a phone
    number -- "Go4Rex", "VERIFY", "INFO". Putting one of those through the
    phone-number normaliser pulls out whatever stray digit it contains: it
    turned "Go4Rex" into "4". Stored in the indexed search column that is
    actively wrong, because a search for a number ending in 4 would then match
    every message the brand ever sent.

    So anything containing a letter has no numeric form, and says so.
    """
    head = str(value or "").split("@", 1)[0]
    if not head or any(ch.isalpha() for ch in head):
        return None
    return normalise_msisdn(head) or None


def _int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def normalise_message(record: dict[str, Any], direction: Direction) -> dict[str, Any]:
    """Map one message from either endpoint onto our columns.

    The whole payload is kept in ``raw``, so a field not modelled yet is never
    lost and a later column can be backfilled from rows already stored.
    """
    if direction is Direction.OUT:
        source = record.get("source_number")
        destination = record.get("destination_number")
        # Outgoing nests the text under `content`; incoming has it at the top.
        content = record.get("content")
        body = content.get("body") if isinstance(content, dict) else record.get("body")
        sent_at = parse_cdr_timestamp(record.get("sent_at"))
        delivered_at = parse_cdr_timestamp(record.get("delivered_at"))
        received_at = None
        # sent_at is the anchor; delivered_at can precede our reading of it but
        # never precedes sending.
        occurred_at = sent_at or delivered_at
        status = record.get("status")
        contact_name = None
    else:
        source = record.get("from") or record.get("source_number")
        destination = record.get("to") or record.get("destination_number")
        body = record.get("body")
        sent_at = None
        delivered_at = None
        received_at = parse_cdr_timestamp(record.get("received_at"))
        occurred_at = received_at
        # An arrived message has no delivery status. Recorded as such rather
        # than invented, so the column means one thing.
        status = None
        contact_name = record.get("contact_name")

    length = _int(record.get("message_length"))
    if length is None and body is not None:
        length = len(body)
    segments = None
    if length:
        segments = max(1, -(-length // SMS_SEGMENT_CHARS))   # ceiling division

    return {
        "message_uuid": str(record.get("message_uuid") or "").strip() or None,
        "direction": direction.value,
        "status": status,
        "sent_at": sent_at,
        "delivered_at": delivered_at,
        "received_at": received_at,
        "occurred_at": occurred_at,
        "source_number": source,
        "source_name": record.get("source_name"),
        "destination_number": destination,
        "source_norm": _numeric_suffix(source),
        "destination_norm": _numeric_suffix(destination),
        "country_code": record.get("country_code"),
        "country_name": record.get("country_name"),
        "contact_name": contact_name,
        "body": body,
        "message_length": length,
        "segments": segments,
        "cost": _decimal(record.get("cost")),
        "platform": record.get("platform"),
        "stream": _name_of(record.get("stream")),
        "campaign": _name_of(record.get("campaign")),
        "conversation": _name_of(record.get("conversation")),
        "external_key": record.get("external_key"),
        "raw": record,
    }


def _name_of(value: Any) -> str | None:
    """These come back either as a bare string or as an object with a name.

    Both shapes appear in the reference's examples, so accept either rather
    than rendering ``{'id': 4, 'name': 'Onboarding'}`` into the page.
    """
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        for key in ("name", "alias", "title", "id"):
            if value.get(key):
                return str(value[key])
        return None
    return str(value)


async def fetch_messages(
    config: SmsApiConfig,
    direction: Direction,
    filters: SmsQueryFilters | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = 500,
) -> list[dict[str, Any]]:
    """Every message matching ``filters``, following the pages.

    ``max_pages`` is a stop, not a tuning knob: a page counter that never
    terminates because the API keeps answering with a full page is an infinite
    loop against someone else's service.
    """
    if not config.token:
        raise ValueError("no TextPeak API key configured")

    filters = filters or SmsQueryFilters()
    if not filters.stream_id and config.stream_id:
        filters.stream_id = config.stream_id

    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=config.timeout_seconds, verify=config.verify_tls
    )
    url = config.base_url.rstrip("/") + config.path_for(direction)
    collected: list[dict[str, Any]] = []
    try:
        page = 1
        while page <= max_pages:
            params = filters.as_params(direction)
            params["page"] = str(page)
            params["itemsPerPage"] = str(config.page_size)
            response = await http.get(url, params=params, headers=config.headers())
            response.raise_for_status()
            payload = response.json()
            items = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(items, list) or not items:
                break
            collected.extend(i for i in items if isinstance(i, dict))
            total = payload.get("total") if isinstance(payload, dict) else None
            if total is not None and len(collected) >= int(total):
                break
            if len(items) < config.page_size:
                break
            page += 1
        else:
            log.warning(
                "sms.page_limit_reached",
                direction=direction.value,
                pages=max_pages,
                collected=len(collected),
            )
    finally:
        if owned:
            await http.aclose()
    return collected


async def store_messages(
    session: AsyncSession,
    brand_id: int,
    rows: list[dict[str, Any]],
    *,
    connection_id: int | None = None,
) -> tuple[int, int]:
    """Upsert normalised messages. Returns ``(inserted, updated)``.

    The update list is deliberately narrow and every field is COALESCEd: a
    later read must be able to fill in a delivery confirmation that had not
    happened yet, and must never blank a value it no longer returns.
    """
    import json

    inserted = updated = 0
    for row in rows:
        if not row.get("message_uuid") or row.get("occurred_at") is None:
            # Without an id there is nothing to deduplicate on, and without a
            # timestamp the row cannot be ordered or paged. Skip rather than
            # invent either.
            log.warning("sms.unusable_record", brand_id=brand_id, uuid=row.get("message_uuid"))
            continue
        result = await session.execute(
            text(
                """
                INSERT INTO sms_messages (
                    brand_id, connection_id, message_uuid, direction, status,
                    sent_at, delivered_at, received_at, occurred_at,
                    source_number, source_name, destination_number,
                    source_norm, destination_norm, country_code, country_name,
                    contact_name, body, message_length, segments, cost,
                    platform, stream, campaign, conversation, external_key, raw
                ) VALUES (
                    :brand_id, :connection_id, :message_uuid, :direction, :status,
                    :sent_at, :delivered_at, :received_at, :occurred_at,
                    :source_number, :source_name, :destination_number,
                    :source_norm, :destination_norm, :country_code, :country_name,
                    :contact_name, :body, :message_length, :segments, :cost,
                    :platform, :stream, :campaign, :conversation, :external_key, :raw
                )
                -- Not RETURNING (xmax = 0): xmax is a system column, and
                -- asyncpg cannot read one from an INSERT routed through a
                -- partitioned parent. See the same note in cdr_client.
                ON CONFLICT (brand_id, message_uuid) DO UPDATE SET
                    status = COALESCE(EXCLUDED.status, sms_messages.status),
                    delivered_at = COALESCE(
                        EXCLUDED.delivered_at, sms_messages.delivered_at
                    ),
                    cost = COALESCE(EXCLUDED.cost, sms_messages.cost),
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
                **row,
                "raw": json.dumps(row["raw"], default=str),
            },
        )
        if result.scalar_one():
            inserted += 1
        else:
            updated += 1
    return inserted, updated


def poll_window(
    last_cursor: datetime | None,
    *,
    now: datetime | None = None,
    overlap_hours: int = 24,
    first_run_days: int = 7,
) -> tuple[datetime, datetime]:
    """The time range for the next poll.

    The overlap is much wider than the CDR poller's minutes because the thing
    that changes late is different in kind: a call record settles seconds after
    hangup, whereas a delivery receipt can arrive hours later, and on some
    routes the next day. Without the overlap those messages keep the status
    they had at first read for ever.
    """
    now = now or datetime.now(UTC)
    if last_cursor is None:
        return now - timedelta(days=first_run_days), now
    return last_cursor - timedelta(hours=overlap_hours), now
