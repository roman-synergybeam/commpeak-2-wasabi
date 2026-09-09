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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _audit_state(event: dict[str, Any]) -> str:
    """Which chip an audit entry belongs to.

    Collapsed to four buckets deliberately: a chip per action would be a row of
    chips nobody reads, and "was anyone refused?" is the question the log is
    actually opened for.
    """
    if str(event.get("result")) != "SUCCESS":
        return "denied"
    action = str(event.get("action") or "").upper()
    if action == "PLAY":
        return "play"
    if action == "DOWNLOAD":
        return "download"
    return "other"


#: The kit's rule again: the database stores USER_CREATED, the page reads
#: "account created". The raw value stays searchable via `_audit_search`, so
#: nothing is lost by making the column readable.
_AUDIT_ACTION_LABEL = {
    "PLAY": "listened",
    "DOWNLOAD": "downloaded",
    "ORGANISATION_CREATED": "organisation created",
    "ORGANISATION_RENAMED": "organisation renamed",
    "TENANT_CREATED": "PBX added",
    "USER_CREATED": "account created",
    "USER_ENABLED": "account enabled",
    "USER_DELETED": "account deleted",
    "USER_ROLE_CHANGED": "role changed",
    "USER_DETAILS_CHANGED": "details changed",
    "PASSWORD_RESET_BY_ADMIN": "password reset by an administrator",
    "USER_BRAND_ADDED": "given access to an organisation",
    "USER_BRAND_REMOVED": "access to an organisation removed",
    "USER_DISABLED": "account disabled",
    "MFA_RESET_BY_ADMIN": "two-factor cleared by an administrator",
    "MFA_ENABLED": "two-factor turned on",
    "MFA_DISABLED": "two-factor turned off",
    "RECOVERY_CODES_REISSUED": "recovery codes reissued",
    "PASSWORD_CHANGED": "password changed",
    "CREDENTIALS_REVEALED": "S3 credentials shown in clear",
}


def _audit_action(value: Any) -> str:
    raw = str(value or "").upper()
    return _AUDIT_ACTION_LABEL.get(raw, raw.replace("_", " ").lower())


def _change_line(entry: dict[str, Any]) -> Markup:
    """One log entry as a sentence.

    Built here rather than in the template because the shape differs by kind --
    a settings change has a before and an after, a media access has a
    recording, an administrative action has a target -- and a template full of
    branches for that is a template nobody can read.

    Returns Markup, so it must escape everything it interpolates itself. A
    filter that forgets is how a page ends up rendering somebody's email
    address as markup.
    """
    action = str(entry.get("action") or "")
    detail = entry.get("detail") or {}
    if not isinstance(detail, dict):
        detail = {}

    if action == "SETTING_CHANGED":
        old_value = entry.get("old_value")
        new_value = entry.get("new_value")
        body = Markup("changed <b>{}</b>").format(str(entry.get("key") or "a setting"))
        if old_value is None and new_value is not None:
            return body + Markup(" to <code>{}</code>").format(new_value)
        if old_value is not None and new_value is None:
            return body + Markup(" back to its default (was <code>{}</code>)").format(old_value)
        return body + Markup(" from <code>{}</code> to <code>{}</code>").format(
            "not set" if old_value is None else old_value,
            "not set" if new_value is None else new_value,
        )

    label = _audit_action(action)
    # A refused entry read "account created ... denied", which says the
    # opposite of what happened until you reach the end of the line.
    if str(entry.get("result") or "SUCCESS") != "SUCCESS":
        label = f"attempted: {label}"
    target = detail.get("target_email")
    if target:
        line = Markup("{} &mdash; <b>{}</b>").format(label, str(target))
    elif entry.get("recording_id"):
        line = Markup("{} recording <b>{}</b>").format(label, str(entry["recording_id"]))
    else:
        line = Markup("{}").format(label)

    # The one or two extras that answer the obvious follow-up question.
    extras: list[str] = []
    changed = detail.get("changed")
    if isinstance(changed, dict):
        extras.extend(
            f"{field}: {values.get('from') or 'not set'} \u2192 {values.get('to') or 'not set'}"
            for field, values in changed.items()
            if isinstance(values, dict)
        )
    if detail.get("from") and detail.get("to"):
        extras.append(f"{detail['from']} \u2192 {detail['to']}")
    if detail.get("role") and not changed:
        extras.append(str(detail["role"]).replace("_", " ").lower())
    if detail.get("reason"):
        extras.append(str(detail["reason"]))
    if extras:
        line += Markup(" <span class=\"det\">({})</span>").format(", ".join(extras))
    return line


def _audit_search(event: dict[str, Any]) -> str:
    """Everything an operator might type when looking for an entry.

    Built on the server, which already has the row -- the client only ever does
    a substring test against it.
    """
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    parts = [
        event.get("actor_label"),
        _audit_action(event.get("action")),
        event.get("action"),
        event.get("result"),
        event.get("call_uuid"),
        event.get("key"),                    # a settings change
        event.get("old_value"),
        event.get("new_value"),
        detail.get("target_email"),
        str(event.get("recording_id") or ""),
        str(event.get("ip") or ""),
    ]
    return " ".join(str(p).lower() for p in parts if p)


def _initials(user: Any) -> str:
    """One or two letters for the account button.

    From the display name where there is one, otherwise the email, because
    "RS" identifies a person at a glance and "roman@synergybeam.com" does not
    fit in 32 pixels.
    """
    name = str(getattr(user, "display_name", "") or "").strip()
    if name:
        parts = [p for p in name.split() if p]
        if len(parts) >= 2:
            return (parts[0][0] + parts[-1][0]).upper()
        if parts:
            return parts[0][:2].upper()
    email = str(getattr(user, "email", "") or "")
    local = email.split("@", 1)[0]
    return (local[:2] or "?").upper()


def _zone_label(zone: Any) -> str:
    """The clock's offset from UTC, as an operator would say it.

    This used to print the last path segment, so the header read "PUERTO RICO"
    beside the time -- which tells you the database row but not the thing you
    actually want from a clock, and reads as a place nobody here is in. The
    zone is configured as "GMT-4" in everyone's head, so the label says
    "GMT-4"; the IANA name stays in the element's title for anyone checking.

    Computed rather than hardcoded, because a zone that observes DST is a
    different offset in July than in January.
    """
    name = str(zone or "UTC")
    try:
        offset = datetime.now(ZoneInfo(name)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return name.rsplit("/", 1)[-1].replace("_", " ")
    if offset is None:
        return "UTC"

    total = int(offset.total_seconds())
    if total == 0:
        return "UTC"
    sign = "+" if total > 0 else "-"
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"GMT{sign}{hours}" if minutes == 0 else f"GMT{sign}{hours}:{minutes:02d}"


#: The kit's rule is that a machine value gets a human label. These are the
#: two enums the people page renders, and "SUPER_ADMIN" is not a job title.
_ROLE_LABEL = {
    "SUPER_ADMIN": "platform admin",
    "ADMIN": "admin",
    "OPERATOR": "operator",
}
_AUTH_LABEL = {
    "LOCAL": "password kept here",
    "ENTRA": "Microsoft Entra ID",
    "GOOGLE": "Google Workspace",
    "LDAP": "Active Directory / LDAP",
}


def _role_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return _ROLE_LABEL.get(str(raw), str(raw).replace("_", " ").lower())


def _auth_label(value: Any) -> str:
    raw = getattr(value, "value", value)
    return _AUTH_LABEL.get(str(raw), str(raw).replace("_", " ").lower())


#: TextPeak's delivery statuses collapsed onto the kit's four pill meanings.
#: The reference documents the field only as "Delivery status" with the example
#: "delivered" and gives no enumeration, so this is a mapping of what has been
#: seen -- anything unrecognised stays `idle` and shows its own text rather
#: than being forced into a colour that would claim something untrue.
_SMS_STATUS_CLASS = {
    "delivered": "ok",
    "delivrd": "ok",           # the raw DLR code some routes return
    "sent": "warn",            # accepted by the carrier, not yet confirmed
    "queued": "warn",
    "pending": "warn",
    "accepted": "warn",
    "submitted": "warn",
    "buffered": "warn",
    "failed": "err",
    "rejected": "err",
    "undelivered": "err",
    "undeliv": "err",
    "expired": "err",
    "error": "err",
    "blocked": "err",
}

#: What each status means, for the title attribute. "sent" and "delivered" look
#: alike and are not: one is the carrier accepting it, the other is the handset
#: confirming it, and the gap between them is where messages get lost.
_SMS_STATUS_HELP = {
    "delivered": "confirmed as arrived on the handset",
    "sent": "accepted by the carrier, delivery not yet confirmed",
    "queued": "waiting to be sent",
    "pending": "waiting on the carrier",
    "failed": "the carrier could not deliver it",
    "rejected": "refused by the carrier or the destination network",
    "undelivered": "did not arrive; the carrier gave up",
    "expired": "the carrier stopped trying before it arrived",
    "blocked": "refused, usually a block on the destination",
}


def _sms_status_class(value: Any) -> str:
    return _SMS_STATUS_CLASS.get(str(value or "").strip().lower(), "idle")


def _sms_status_help(value: Any) -> str:
    return _SMS_STATUS_HELP.get(str(value or "").strip().lower(), "")


def _json_attr(value: Any) -> str:
    """JSON for an HTML *attribute*, left as plain text so Jinja escapes it.

    Deliberately not :func:`markupsafe`-marked. Jinja's own ``tojson`` escapes
    ``<``, ``>``, ``&`` and ``'`` but not ``"``, which is right inside a
    ``<script>`` and broken inside ``value="..."`` -- the first quote of the
    JSON would close the attribute. Here autoescaping does the quoting.

    ``tojson`` itself is left alone: it used to be overridden with a bare
    ``json.dumps``, whose output autoescaping then turned into ``&#34;`` inside
    a ``<script>``. That is a JavaScript syntax error, and it took the whole
    block with it -- the header clock stopped at ``--:--:--`` and the account
    menu stopped closing, from one filter registration.
    """
    return json.dumps(value)


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
            "initials": _initials,
            "zone_label": _zone_label,
            "json_attr": _json_attr,
            "role_label": _role_label,
            "sms_status_class": _sms_status_class,
            "sms_status_help": _sms_status_help,
            "auth_label": _auth_label,
            "audit_state": _audit_state,
            "audit_action": _audit_action,
            "change_line": _change_line,
            "audit_search": _audit_search,
        }
    )
