"""CommPeak CDR API client and ingest.

The field names here come from a real CDR payload, so the mapping is concrete::

    {"object_type_id":4, "start_at":"2026-09-08 00:52:17", "end_at":"...",
     "src":"593990899917@did.commpeak.com", "dst":"0007281",
     "call_uuid":"e9a46b6f-...", "call_duration":35, "status":"NORMAL_CLEARING",
     "call_id":113332, "caller_user":"System",
     "public_recording_url":"https://<tenant>/record/public/113332/<sha1>/as/mp3", ...}

What is *not* pinned down is the transport: the endpoint path, the
authentication scheme and the pagination style differ between CommPeak
deployments, and the published reference for ``getAllCdrs`` was unavailable when
this was written.  So those are configuration, and the response parsing is
written defensively -- it accepts a bare list, ``{"data": [...]}`` or
``{"items": [...]}``, and tolerates missing fields rather than refusing a whole
page because one column was renamed.

Timestamps arrive without a zone (``2026-09-08 00:52:17``).  They are read as
UTC, which is consistent with the recording filenames: the documented example
key's channel id decodes to exactly its wall-clock field in UTC.
"""

from __future__ import annotations

import enum
import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    "ingest_page",
    "normalise_cdr",
    "parse_cdr_timestamp",
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
    """How to reach one tenant's CDR API.

    Deliberately generic: until the exact contract is confirmed, an operator can
    point this at the right endpoint without a code change.
    """

    base_url: str
    path: str = "/api/v1/cdrs"
    auth: AuthScheme = AuthScheme.BEARER
    token: str = ""
    username: str = ""
    #: Query parameter names, which vary between deployments.
    param_from: str = "start_at_from"
    param_to: str = "start_at_to"
    param_limit: str = "limit"
    param_offset: str = "offset"
    header_name: str = "X-API-Key"
    page_size: int = DEFAULT_PAGE_SIZE
    verify_tls: bool = True


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
    """Infer call direction.

    CommPeak does not send a direction field in the observed payload, but an
    ``@did.commpeak.com`` source is an inbound DID leg, which is the same signal
    the recording filenames encode as ``in``/``out``.
    """
    src = str(record.get("src") or "")
    if "@did." in src:
        return "in"
    if str(record.get("object_type_id") or "") == "4":
        return "in"
    dst = str(record.get("dst") or "")
    if "@" in dst:
        return "out"
    return None


def normalise_cdr(record: dict[str, Any]) -> dict[str, Any]:
    """Map one API record onto our column names.

    The full payload is kept in ``raw`` so a field we did not model -- or one
    CommPeak adds later -- is never lost, and ``src_norm``/``dst_norm`` are
    computed on write so number search and correlation never pay for
    normalisation per row.
    """
    start_at = parse_cdr_timestamp(record.get("start_at"))
    end_at = parse_cdr_timestamp(record.get("end_at"))
    src = record.get("src")
    dst = record.get("dst")
    recording_url = record.get("public_recording_url") or record.get("record_file")

    return {
        "call_uuid": record.get("call_uuid"),
        "call_id": record.get("call_id"),
        "start_at": start_at,
        "end_at": end_at,
        "call_duration": record.get("call_duration"),
        "direction": _direction_of(record),
        "src": src,
        "dst": dst,
        "src_norm": normalise_msisdn(src) or None,
        "dst_norm": normalise_msisdn(dst) or None,
        "dst_country": record.get("dst_country"),
        "agent_extension": record.get("agent_callerid_number"),
        "agent_name": record.get("agent_callerid_name"),
        "caller_user": record.get("caller_user") or record.get("caller_username"),
        "client_callerid_name": record.get("client_callerid_name"),
        "client_callerid_number": record.get("client_callerid_number"),
        "status": record.get("status"),
        "hangup_disposition": record.get("client_hangup_disposition")
        or record.get("agent_hangup_disposition"),
        "public_recording_url": recording_url,
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
    """Reads CDRs for one connection."""

    def __init__(self, config: CdrApiConfig) -> None:
        self.config = config

    def _auth_bits(self) -> tuple[dict[str, str], dict[str, str], httpx.Auth | None]:
        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, str] = {}
        auth: httpx.Auth | None = None
        match self.config.auth:
            case AuthScheme.BEARER:
                headers["Authorization"] = f"Bearer {self.config.token}"
            case AuthScheme.HEADER:
                headers[self.config.header_name] = self.config.token
            case AuthScheme.QUERY:
                params["api_key"] = self.config.token
            case AuthScheme.BASIC:
                auth = httpx.BasicAuth(self.config.username, self.config.token)
            case AuthScheme.NONE:
                pass
        return headers, params, auth

    async def fetch_range(
        self, start: datetime, end: datetime, *, max_pages: int = 500
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield pages of raw CDR records covering ``[start, end]``.

        ``max_pages`` bounds a run: a misconfigured pagination parameter that
        the API ignores would otherwise loop forever re-reading page one.
        """
        headers, base_params, auth = self._auth_bits()
        url = self.config.base_url.rstrip("/") + "/" + self.config.path.lstrip("/")

        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=self.config.verify_tls, auth=auth
        ) as client:
            offset = 0
            for _page in range(max_pages):
                params = {
                    **base_params,
                    self.config.param_from: start.strftime("%Y-%m-%d %H:%M:%S"),
                    self.config.param_to: end.strftime("%Y-%m-%d %H:%M:%S"),
                    self.config.param_limit: str(self.config.page_size),
                    self.config.param_offset: str(offset),
                }
                response = await client.get(url, params=params, headers=headers)
                if response.status_code == 401:
                    raise PermissionError("CDR API rejected the credentials (401)")
                if response.status_code == 403:
                    raise PermissionError(
                        "CDR API returned 403; check the API user's permissions and IP allow-list"
                    )
                response.raise_for_status()

                records = _extract_records(response.json())
                if not records:
                    return
                yield records
                if len(records) < self.config.page_size:
                    return
                offset += len(records)
            log.warning(
                "cdr.page_limit_reached",
                max_pages=max_pages,
                detail="stopping; check that the offset parameter is honoured",
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
                    caller_user, client_callerid_name, client_callerid_number,
                    status, hangup_disposition, public_recording_url, raw
                ) VALUES (
                    :brand_id, :connection_id, :tenant_id, :call_uuid, :call_id,
                    :start_at, :end_at, :call_duration, :direction, :src, :dst,
                    :src_norm, :dst_norm, :dst_country, :agent_extension, :agent_name,
                    :caller_user, :client_callerid_name, :client_callerid_number,
                    :status, :hangup_disposition, :public_recording_url, :raw
                )
                ON CONFLICT (brand_id, connection_id, call_uuid) DO UPDATE SET
                    end_at = COALESCE(EXCLUDED.end_at, cdrs.end_at),
                    call_duration = COALESCE(EXCLUDED.call_duration, cdrs.call_duration),
                    status = COALESCE(EXCLUDED.status, cdrs.status),
                    hangup_disposition = COALESCE(
                        EXCLUDED.hangup_disposition, cdrs.hangup_disposition
                    ),
                    public_recording_url = COALESCE(
                        EXCLUDED.public_recording_url, cdrs.public_recording_url
                    ),
                    raw = EXCLUDED.raw,
                    updated_at = now()
                RETURNING (xmax = 0) AS was_inserted
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
