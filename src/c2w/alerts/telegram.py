"""Telegram alert channel, with two audiences.

An alert has two possible readers and they want different things:

* the **organisation** chat, which should see that company's calls, accounts
  and failures and nothing from any other company;
* the **platform** chat, which watches the whole system and needs everything,
  each message naming which organisation it came from.

One chat cannot be both. Pointing an organisation at the platform chat would
show it another company's names, which is the isolation rule this platform is
built around; and giving the platform only one organisation's chat means the
other company's failures reach nobody responsible for the system.

So a message is posted to at most two chats, and the *same* chat is never
posted to twice -- which matters in practice, because the common setup is one
chat configured globally, inherited by every organisation and also serving as
the platform chat. Without the de-duplication that arrangement delivers
everything twice.
"""

from __future__ import annotations

import httpx

from c2w.alerts.base import Alert, Severity
from c2w.logging import get_logger
from c2w.settings import settings_service

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0)

#: Ordered, so "at least WARNING" is a comparison rather than a set.
_RANK: dict[str, int] = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


def _passes(minimum: str, severity: Severity) -> bool:
    return _RANK.get(str(severity), 0) >= _RANK.get(minimum.upper(), 0)


async def _post(token: str, chat_id: str, text: str) -> bool:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )
    if response.status_code >= 400:
        # The status, never the token -- it is in the URL, so the URL is never
        # logged either. Nor the chat id, which identifies a private group.
        log.warning("alert.telegram_rejected", status=response.status_code)
        return False
    return True


async def send(session, alert: Alert) -> bool:
    """Post an alert to the organisation chat, the platform chat, or both.

    Returns True if it reached at least one.
    """
    org_token = await settings_service.get_secret(
        session, "alerts.telegram_bot_token", brand_id=alert.brand_id
    )
    org_chat = await settings_service.get_str(
        session, "alerts.telegram_chat_id", brand_id=alert.brand_id
    )

    platform_chat = await settings_service.get_secret(
        session, "alerts.telegram_platform_chat_id"
    )
    # Falls back to the organisation bot: one bot can post to many chats, so
    # demanding a second token to use a second chat would be an obstacle with
    # no purpose.
    platform_token = (
        await settings_service.get_secret(session, "alerts.telegram_platform_bot_token")
    ) or org_token
    platform_minimum = (
        await settings_service.get_str(session, "alerts.telegram_platform_min_severity")
    ) or "INFO"

    delivered = False
    already: set[str] = set()

    platform_wanted = bool(
        platform_token and platform_chat and _passes(platform_minimum, alert.severity)
    )

    if org_token and org_chat:
        # When the same chat is *also* the platform chat, it gets the platform
        # rendering. The de-duplication below drops the second post, and the
        # organisation copy goes first, so rendering this one as the
        # organisation copy silently loses every platform-only field in the
        # commonest setup there is -- one chat configured globally and
        # inherited by everything. Whoever configured one chat for both roles
        # *is* the platform reader; the fields are withheld from a separate,
        # company-facing chat, which is the case the isolation rule is about.
        both = platform_wanted and platform_chat == org_chat
        if await _post(org_token, org_chat, alert.as_text(for_platform=both)):
            delivered = True
        already.add(org_chat)

    if platform_wanted and platform_chat not in already:
        # The platform reader is watching several companies at once, so the
        # message has to say which one this is even when the alert itself did
        # not bother -- an unattributed "3 transfers failed" is useless to them.
        prefix = "" if alert.brand_name else "Platform-wide\n"
        # The platform copy is rendered separately, not prefixed: it may carry
        # fields the organisation copy must not (see `Alert.platform_fields`).
        if await _post(
            platform_token, platform_chat, prefix + alert.as_text(for_platform=True)
        ):
            delivered = True

    return delivered
