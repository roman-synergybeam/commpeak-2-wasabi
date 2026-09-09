"""Jinja filters.

Formatting lives here rather than in templates so it is testable and
consistent: a duration, a state pill or a byte count should look the same
everywhere it appears.

Two rules from the design kit are enforced by these maps rather than left to
whoever writes the next template:

*State colour is a vocabulary, not decoration.* There are exactly four pill
meanings -- ok / warn / err / idle -- and every state in the system collapses
into one of them. A fifth colour would devalue the four that mean something.

*Machine values get human labels.* The database stores ``MISSING_SOURCE``;
an operator reads "gone from CommPeak". The raw value stays available where it
matters (the settings key, the technical detail panel), but it is not what the
page leads with.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode

from markupsafe import Markup

__all__ = ["register"]

# ---------------------------------------------------------------- recordings

#: Recording state -> one of the kit's four pill meanings.
_STATE_CLASS = {
    "AVAILABLE": "ok",
    "SOURCE_DELETED": "ok",
    "VERIFIED": "ok",
    "UPLOADED": "warn",
    "TRANSFERRING": "warn",
    "QUEUED": "idle",
    "DISCOVERED": "idle",
    "FAILED": "err",
    "MISSING_SOURCE": "err",
}

_STATE_LABEL = {
    "DISCOVERED": "found",
    "QUEUED": "waiting",
    "TRANSFERRING": "copying",
    "UPLOADED": "checking",
    "VERIFIED": "verified",
    "AVAILABLE": "playable",
    "SOURCE_DELETED": "archived only",
    "FAILED": "failed",
    "MISSING_SOURCE": "gone from source",
}

_STATE_HELP = {
    "DISCOVERED": "seen at CommPeak, not yet due for copying",
    "QUEUED": "waiting for a worker to pick it up",
    "TRANSFERRING": "being copied to the archive now",
    "UPLOADED": "copied; the archive copy is being checked",
    "VERIFIED": "the archive copy is confirmed byte-for-byte",
    "AVAILABLE": "verified and playable",
    "SOURCE_DELETED": "kept in the archive; no longer held at CommPeak",
    "FAILED": "gave up after repeated errors",
    "MISSING_SOURCE": "removed at CommPeak before it could be copied",
}

#: How confidently a recording was matched to its call.
_MATCH_CLASS = {
    "epoch_exact": "ok",
    "epoch_only": "ok",
    "time_number": "warn",
    "time_only": "warn",
    "orphan": "idle",
}

_MATCH_LABEL = {
    "epoch_exact": "matched exactly",
    "epoch_only": "matched on time",
    "time_number": "matched closely",
    "time_only": "matched loosely",
    "orphan": "no matching call",
}

# CommPeak/FreeSWITCH hangup causes. NORMAL_CLEARING is a completed call.
_STATUS_CLASS = {
    "NORMAL_CLEARING": "ok",
    "ANSWERED": "ok",
    "USER_BUSY": "warn",
    "NO_ANSWER": "warn",
    "NO_USER_RESPONSE": "warn",
    "ORIGINATOR_CANCEL": "idle",
    "CALL_REJECTED": "err",
    "UNALLOCATED_NUMBER": "err",
}

_STATUS_LABEL = {
    "NORMAL_CLEARING": "answered",
    "NO_USER_RESPONSE": "no answer",
    "ORIGINATOR_CANCEL": "cancelled",
    "UNALLOCATED_NUMBER": "bad number",
}

_CONNECTION_CLASS = {
    "OK": "ok",
    "ERROR": "err",
    "DEGRADED": "warn",
    "UNTESTED": "idle",
    "DISABLED": "idle",
}

_ERROR_LABEL = {
    "ACL_ERROR": "address not allowed",
    "AUTH_ERROR": "wrong credentials",
    "CONFIG_ERROR": "misconfigured",
    "NOT_FOUND": "vanished at source",
    "RATE_LIMIT": "throttled",
    "NETWORK_ERROR": "network",
    "S3_ERROR": "storage error",
    "CHECKSUM_ERROR": "bytes disagreed",
    "STORAGE_ERROR": "archive refused",
    "PERMISSION_ERROR": "not permitted",
}

_RUN_LABEL = {
    "FULL_INVENTORY": "full scan",
    "INCREMENTAL": "scan for new recordings",
    "CDR_POLL": "fetch call records",
    "RECONCILE": "nightly archive check",
    "RETENTION": "queue recordings due for archiving",
}


# ------------------------------------------------------------------ formatting


def _filesize(value: Any) -> str:
    """Human byte sizes. TB matters here: these buckets hold terabytes."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} PB"


def _duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _dt(value: Any) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else "—"


def _dtlocal(value: Any) -> str:
    """Format for an ``<input type=datetime-local>``."""
    return value.strftime("%Y-%m-%dT%H:%M") if isinstance(value, datetime) else ""


def _number(value: Any) -> str:
    """Trim the ``@domain`` noise from a CDR number for display.

    The full value stays in the detail view; a hundred rows are unreadable with
    ``@did.commpeak.com`` repeated down the page.
    """
    return str(value).split("@", 1)[0] if value else "—"


def _media_pill(row: dict[str, Any]) -> Markup:
    """One pill summarising a call's recordings.

    Returns Markup because this is HTML: as a plain string Jinja would escape
    it and the table would show literal angle brackets. The dynamic parts go
    through ``Markup.format``, which escapes its arguments.
    """
    parts = int(row.get("recording_parts") or 0)
    if not parts:
        return Markup('<span class="pill idle">none</span>')

    states = set(row.get("recording_states") or [])
    if states & {"AVAILABLE", "SOURCE_DELETED"}:
        style, label = "ok", "playable"
    elif states & {"FAILED", "MISSING_SOURCE"}:
        style, label = "err", "failed"
    else:
        style, label = "warn", "copying"

    # A multi-part call is shown as "playable x3"; CommPeak splits long calls.
    suffix = f" \u00d7{parts}" if parts > 1 else ""
    return Markup('<span class="pill {}">{}{}</span>').format(style, label, suffix)


def _archived_pct(row: Any) -> float:
    """Share of a connection's recordings that are verified in the archive."""
    total = (row.get("total") if hasattr(row, "get") else 0) or 0
    archived = (row.get("archived") if hasattr(row, "get") else 0) or 0
    return round(100 * archived / total, 1) if total else 0.0


def _meter_class(pct: Any) -> str:
    """Colour a meter by how far along it is.

    Applied to the fill *and* the percentage, so the figure still carries the
    meaning when the bar is not read.
    """
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return ""
    if value >= 99:
        return ""
    if value < 50:
        return "bad"
    if value < 90:
        return "warn"
    return ""


# ------------------------------------------------------------------- querystring


def _sortlink(query_string: str, field: str) -> str:
    """Toggle sort direction for a column heading."""
    params = parse_qs(query_string, keep_blank_values=False)
    current = params.get("sort", ["started"])[0]
    descending = params.get("desc", ["1"])[0] in ("1", "true")
    params["sort"] = [field]
    params["desc"] = ["0" if (current == field and descending) else "1"]
    params.pop("offset", None)
    return urlencode(params, doseq=True)


def _offsetlink(query_string: str, offset: int) -> str:
    params = parse_qs(query_string, keep_blank_values=False)
    params["offset"] = [str(max(0, offset))]
    return urlencode(params, doseq=True)


def _chiplink(query_string: str, key: str, value: str) -> str:
    """Set one filter and return to the first page."""
    params = parse_qs(query_string, keep_blank_values=False)
    if value:
        params[key] = [value]
    else:
        params.pop(key, None)
    params.pop("offset", None)
    return urlencode(params, doseq=True)


def register(env: Any) -> None:
    env.filters.update(
        {
            "filesize": _filesize,
            "duration": _duration,
            "dt": _dt,
            "dtlocal": _dtlocal,
            "number": _number,
            "media_pill": _media_pill,
            "archived_pct": _archived_pct,
            "meter_class": _meter_class,
            "state_class": lambda v: _STATE_CLASS.get(str(v), "idle"),
            "state_label": lambda v: _STATE_LABEL.get(str(v), str(v).lower()),
            "state_help": lambda v: _STATE_HELP.get(str(v), ""),
            "match_class": lambda v: _MATCH_CLASS.get(str(v), "idle"),
            "match_label": lambda v: _MATCH_LABEL.get(str(v), str(v)),
            "status_class": lambda v: _STATUS_CLASS.get(str(v), "idle"),
            "shorten_status": lambda v: _STATUS_LABEL.get(
                str(v), str(v or "").replace("_", " ").lower()
            ),
            "conn_class": lambda v: _CONNECTION_CLASS.get(str(v), "idle"),
            "error_label": lambda v: _ERROR_LABEL.get(str(v), str(v).replace("_", " ").lower()),
            "run_label": lambda v: _RUN_LABEL.get(str(v), str(v).replace("_", " ").lower()),
            "sortlink": _sortlink,
            "offsetlink": _offsetlink,
            "chiplink": _chiplink,
            "tojson": json.dumps,
        }
    )
