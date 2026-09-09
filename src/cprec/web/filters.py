"""Jinja filters.

Formatting lives here rather than in templates so it is testable and consistent
-- a duration or a state badge should look the same everywhere it appears.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode

from markupsafe import Markup

__all__ = ["register"]

_STATE_CLASS = {
    "AVAILABLE": "ok",
    "SOURCE_DELETED": "ok",
    "VERIFIED": "ok",
    "UPLOADED": "info",
    "TRANSFERRING": "info",
    "QUEUED": "mute",
    "DISCOVERED": "mute",
    "FAILED": "err",
    "MISSING_SOURCE": "err",
}

_STATE_HELP = {
    "DISCOVERED": "found at source, not yet queued for archiving",
    "QUEUED": "waiting for a worker",
    "TRANSFERRING": "being copied to the archive",
    "UPLOADED": "copied, awaiting verification",
    "VERIFIED": "bytes confirmed in the archive",
    "AVAILABLE": "verified and playable",
    "SOURCE_DELETED": "archived; source copy no longer present",
    "FAILED": "gave up after repeated errors",
    "MISSING_SOURCE": "vanished from CommPeak before it could be copied",
}

_MATCH_CLASS = {
    "epoch_exact": "ok",
    "epoch_only": "ok",
    "time_number": "info",
    "time_only": "warn",
    "orphan": "mute",
}

# CommPeak/FreeSWITCH hangup causes. ANSWERED-equivalent is NORMAL_CLEARING.
_STATUS_CLASS = {
    "NORMAL_CLEARING": "ok",
    "ANSWERED": "ok",
    "USER_BUSY": "warn",
    "NO_ANSWER": "warn",
    "NO_USER_RESPONSE": "warn",
    "ORIGINATOR_CANCEL": "mute",
    "CALL_REJECTED": "err",
    "UNALLOCATED_NUMBER": "err",
}


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
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _dt(value: Any) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _dtlocal(value: Any) -> str:
    """Format for an ``<input type=datetime-local>``."""
    if not isinstance(value, datetime):
        return ""
    return value.strftime("%Y-%m-%dT%H:%M")


def _number(value: Any) -> str:
    """Trim the ``@domain`` noise from a CDR number for display.

    The full value stays available in the detail view; a table of a hundred rows
    is unreadable with ``@did.commpeak.com`` repeated on every line.
    """
    if not value:
        return "—"
    return str(value).split("@", 1)[0]


def _shorten_status(value: Any) -> str:
    text = str(value or "")
    return {"NORMAL_CLEARING": "answered", "NO_USER_RESPONSE": "no answer"}.get(
        text, text.replace("_", " ").lower()
    )


def _media_badge(row: dict[str, Any]) -> Markup:
    """One badge summarising a call's recordings.

    Returns Markup because this is HTML: as a plain string Jinja would escape it
    and the table would show literal angle brackets. The dynamic parts go
    through ``Markup.format``, which escapes its arguments, so this cannot
    become an injection point even if the shape of a row changes.

    A call whose recording failed to correlate still shows its recording, and a
    call with no recording says so plainly rather than looking broken.
    """
    parts = int(row.get("recording_parts") or 0)
    if not parts:
        return Markup('<span class="badge mute">none</span>')

    states = set(row.get("recording_states") or [])
    if states & {"AVAILABLE", "SOURCE_DELETED"}:
        style, label = "ok", "playable"
    elif states & {"FAILED", "MISSING_SOURCE"}:
        style, label = "err", "failed"
    else:
        style, label = "info", "syncing"

    # A multi-part call is shown as "playable x3"; CommPeak splits long calls.
    suffix = f" \u00d7{parts}" if parts > 1 else ""
    template = Markup('<span class="badge {}">{}{}</span>')
    return template.format(style, label, suffix)


def _sortlink(query_string: str, field: str) -> str:
    """Toggle sort direction for a column header."""
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


def register(env: Any) -> None:
    env.filters.update(
        {
            "filesize": _filesize,
            "duration": _duration,
            "dt": _dt,
            "dtlocal": _dtlocal,
            "number": _number,
            "shorten_status": _shorten_status,
            "state_class": lambda v: _STATE_CLASS.get(str(v), "mute"),
            "state_help": lambda v: _STATE_HELP.get(str(v), ""),
            "match_class": lambda v: _MATCH_CLASS.get(str(v), "mute"),
            "status_class": lambda v: _STATUS_CLASS.get(str(v), "mute"),
            "conn_class": lambda v: {
                "OK": "ok",
                "ERROR": "err",
                "DEGRADED": "warn",
                "UNTESTED": "mute",
                "DISABLED": "mute",
            }.get(str(v), "mute"),
            "media_badge": _media_badge,
            "sortlink": _sortlink,
            "offsetlink": _offsetlink,
            "tojson": lambda v: json.dumps(v),
        }
    )
