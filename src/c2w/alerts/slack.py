"""Slack alert channel."""

from __future__ import annotations

import httpx

from c2w.alerts.base import Alert, Severity
from c2w.logging import get_logger
from c2w.settings import settings_service

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)
_COLOURS = {Severity.INFO: "#36a64f", Severity.WARNING: "#daa038", Severity.CRITICAL: "#d00000"}


async def send(session, alert: Alert) -> bool:
    """Post an alert to a Slack incoming webhook.  False when not configured."""
    webhook = await settings_service.get_secret(
        session, "alerts.slack_webhook_url", brand_id=alert.brand_id
    )
    if not webhook:
        return False

    payload = {
        "attachments": [
            {
                "color": _COLOURS[alert.severity],
                "title": f"{alert.severity.emoji} {alert.title}",
                "text": alert.body,
                "fields": [
                    {"title": k, "value": v, "short": True} for k, v in alert.fields.items()
                ],
                "footer": alert.brand_name or "c2w",
            }
        ]
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(webhook, json=payload)
    if response.status_code >= 400:
        log.warning("alert.slack_rejected", status=response.status_code)
        return False
    return True
