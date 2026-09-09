"""Telegram alert channel."""

from __future__ import annotations

import httpx

from cprec.alerts.base import Alert
from cprec.logging import get_logger
from cprec.settings import settings_service

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)


async def send(session, alert: Alert) -> bool:
    """Post an alert to Telegram.  Returns False when not configured."""
    token = await settings_service.get_secret(
        session, "alerts.telegram_bot_token", brand_id=alert.brand_id
    )
    chat_id = await settings_service.get_str(
        session, "alerts.telegram_chat_id", brand_id=alert.brand_id
    )
    if not token or not chat_id:
        return False

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": alert.as_text(),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
    if response.status_code >= 400:
        # Log the status, never the token -- the URL contains it, so the URL is
        # never logged either.
        log.warning("alert.telegram_rejected", status=response.status_code)
        return False
    return True
