"""Alert dispatch, shared by the Telegram and Slack channels.

Two behaviours matter more than the transports themselves:

*Deduplication.*  A CommPeak ACL problem fails every queued job for that
connection.  Without suppression that is thousands of identical messages, which
in practice trains people to mute the channel -- so an alert storm is worse than
no alerting.  Identical alerts are collapsed within a window.

*Never let alerting break the work.*  A wrong Slack webhook must not fail a
transfer that actually succeeded, so dispatch failures are logged and swallowed.
"""

from __future__ import annotations

import enum
import hashlib
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from cprec.logging import get_logger
from cprec.settings import settings_service

log = get_logger(__name__)

__all__ = ["Alert", "Severity", "dispatch"]


class Severity(enum.StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

    @property
    def emoji(self) -> str:
        return {"INFO": "\u2139\ufe0f", "WARNING": "\u26a0\ufe0f", "CRITICAL": "\U0001f6a8"}[
            self.value
        ]


@dataclass(slots=True)
class Alert:
    title: str
    body: str
    severity: Severity = Severity.WARNING
    brand_id: int | None = None
    brand_name: str | None = None
    #: Groups repeats of the same condition. Defaults to title+brand, which is
    #: usually the right granularity: one alert per problem per brand.
    dedupe_key: str | None = None
    fields: dict[str, str] = field(default_factory=dict)

    def key(self) -> str:
        raw = self.dedupe_key or f"{self.severity}:{self.title}:{self.brand_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def as_text(self) -> str:
        lines = [f"{self.severity.emoji} *{self.title}*"]
        if self.brand_name:
            lines.append(f"Brand: {self.brand_name}")
        if self.body:
            lines.append("")
            lines.append(self.body)
        if self.fields:
            lines.append("")
            lines.extend(f"{k}: {v}" for k, v in self.fields.items())
        return "\n".join(lines)


#: Process-local suppression. Deliberately not in the database: an alert storm
#: is exactly when we do not want every worker writing rows, and a duplicate
#: from a second process is a far smaller problem than thousands from one.
_last_sent: dict[str, float] = {}


async def dispatch(session: AsyncSession, alert: Alert) -> list[str]:
    """Send an alert to every configured channel.

    Returns the channels that accepted it.  Never raises: alerting is
    observability, and observability failing must not take work with it.
    """
    if not await settings_service.get_bool(session, "alerts.enabled"):
        return []

    window = await settings_service.get_int(session, "alerts.dedupe_window_seconds")
    key = alert.key()
    now = time.monotonic()
    if window > 0 and now - _last_sent.get(key, 0.0) < window:
        log.debug("alert.suppressed", title=alert.title, dedupe_key=key)
        return []

    sent: list[str] = []
    from cprec.alerts import slack, telegram

    for name, sender in (("telegram", telegram.send), ("slack", slack.send)):
        try:
            if await sender(session, alert):
                sent.append(name)
        except Exception as exc:
            log.warning("alert.dispatch_failed", channel=name, error=str(exc))

    if sent:
        _last_sent[key] = now
        log.info("alert.sent", title=alert.title, severity=str(alert.severity), channels=sent)
    return sent


def reset_dedupe_cache() -> None:
    """Clear suppression state.  For tests."""
    _last_sent.clear()
