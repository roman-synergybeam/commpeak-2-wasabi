"""Proving a settings card actually works, from the card itself.

Every one of these sections is a set of values copied out of somebody else's
console, and the failure mode is always the same: it looks configured and does
nothing. A token with a trailing space, a chat id from the wrong group, a
domain controller reachable from the office but not from this host -- none of
them announce themselves, and all of them are invisible until the thing they
were needed for silently does not happen.

So each section that talks to something outside this box gets a button that
talks to it now, and reports what came back.

Two rules the tests here follow:

* **Read-only wherever a read exists.** The CommPeak checks list and fetch;
  they never write, because writing to CommPeak is not something this software
  is allowed to do at all.
* **A send is announced as a send.** Telegram and Slack cannot be tested
  without delivering something, so their buttons say so and the message says
  it is a test. A "test" that quietly messages a customer channel would be a
  nasty surprise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.settings import settings_service

__all__ = ["TESTABLE_SECTIONS", "SectionTest", "run_section_test"]

log = get_logger(__name__)

#: Which cards get a button, and what the button should say. A section absent
#: from here has nothing outside this box to talk to, so a test would only be
#: able to tell you what the form already shows.
TESTABLE_SECTIONS: dict[str, str] = {
    "CommPeak calls": "Test the call records connection",
    "CommPeak messages": "Test the messages connection",
    "Alerts": "Send a test alert",
    "Active Directory": "Test the directory connection",
    "Cloudflare": "Check the Turnstile keys",
}


@dataclass
class SectionTest:
    """What a section's test found. Never raises past the route."""

    ok: bool
    summary: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str = "", hint: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})


async def run_section_test(
    session: AsyncSession, category: str, *, brand_id: int | None, actor: str
) -> SectionTest:
    """Dispatch to the check for one settings card."""
    runner = {
        "CommPeak calls": _test_cdr_api,
        "CommPeak messages": _test_textpeak,
        "Alerts": _test_alerts,
        "Active Directory": _test_directory,
        "Cloudflare": _test_turnstile,
    }.get(category)
    if runner is None:
        return SectionTest(False, "There is nothing to test on this page.")
    try:
        return await runner(session, brand_id, actor)
    except Exception as exc:  # every failure is a message, not a 500
        log.warning("settings.test_failed", category=category, error=str(exc)[:200])
        return SectionTest(False, f"The test could not run: {exc}")


async def _test_cdr_api(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Ask PBX Stats for one call record. Read-only."""
    from datetime import UTC, datetime, timedelta

    from c2w.commpeak.cdr_client import AuthScheme, CdrApiConfig, CdrClient

    base = (await settings_service.get_str(session, "commpeak.cdr_api_base", brand_id=brand_id)
            or "").strip()
    token = await settings_service.get_secret(
        session, "commpeak.cdr_api_token", brand_id=brand_id
    )
    out = SectionTest(True, "")
    if not base:
        out.ok = False
        out.summary = "No call records address is set, so there is nothing to reach."
        out.add("address", False, "empty",
                "Your PBX Stats host, like https://yourname.stats.pbx.commpeak.com")
        return out
    out.add("address", True, base)
    if not token:
        out.ok = False
        out.summary = "No API key is set for the call records address."
        out.add("api key", False, "not set", "From the same CommPeak console page")
        return out
    out.add("api key", True, "stored")

    scheme = await settings_service.get_str(
        session, "commpeak.cdr_auth_scheme", brand_id=brand_id
    )
    config = CdrApiConfig(
        base_url=base,
        path=await settings_service.get_str(session, "commpeak.cdr_api_path", brand_id=brand_id),
        auth=AuthScheme(str(scheme or "header")),
        token=token,
        username=await settings_service.get_str(
            session, "commpeak.cdr_api_user", brand_id=brand_id
        ),
        page_size=1,
    )
    # A day is enough to prove the connection without asking the instance for
    # a month of history it then has to page through.
    end = datetime.now(UTC)
    start = end - timedelta(days=1)
    try:
        records: list[dict[str, Any]] = []
        client = CdrClient(config)
        async for page in client.fetch_range(start, end, max_pages=1):
            records = page
            break
    except httpx.HTTPStatusError as exc:
        out.ok = False
        code = exc.response.status_code
        out.summary = f"The call records address answered {code}."
        out.add(
            "fetch one record", False, f"HTTP {code}",
            "401 or 403 means the API key is wrong or not enabled for this "
            "instance; 404 usually means the host is not a PBX Stats instance."
        )
        return out
    except Exception as exc:
        out.ok = False
        out.summary = f"Could not reach the call records address: {exc}"
        out.add("fetch one record", False, str(exc)[:140])
        return out

    out.add("fetch one record", True, f"{len(records)} returned")
    out.summary = (
        f"Reached the call records API and read {len(records)} record(s)."
        if records
        else "Reached the call records API. It answered, with no calls in the default range."
    )
    return out


async def _test_textpeak(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Ask TextPeak for one message. Read-only."""
    from c2w.commpeak.sms_client import Direction, SmsApiConfig, SmsQueryFilters, fetch_messages

    out = SectionTest(True, "")
    token = await settings_service.get_secret(session, "sms.api_token", brand_id=brand_id)
    if not token:
        out.ok = False
        out.summary = "No TextPeak API key is set."
        out.add("api key", False, "not set", "This is a different key from the call records one")
        return out
    out.add("api key", True, "stored")

    config = SmsApiConfig(
        token=token,
        base_url=await settings_service.get_str(session, "sms.api_base", brand_id=brand_id),
        outgoing_path=await settings_service.get_str(session, "sms.api_path", brand_id=brand_id),
        incoming_path=await settings_service.get_str(
            session, "sms.incoming_path", brand_id=brand_id
        ),
        stream_id=await settings_service.get_str(session, "sms.stream_id", brand_id=brand_id),
        page_size=1,
    )
    for direction, label in ((Direction.OUT, "sent messages"), (Direction.IN, "received messages")):
        try:
            items = await fetch_messages(config, direction, SmsQueryFilters(), max_pages=1)
            out.add(label, True, f"{len(items)} returned")
        except httpx.HTTPStatusError as exc:
            out.ok = False
            out.add(label, False, f"HTTP {exc.response.status_code}",
                    "401 or 403 means the key is wrong; the key goes in the "
                    "Authorization header on its own, with no 'Bearer' prefix.")
        except Exception as exc:
            out.ok = False
            out.add(label, False, str(exc)[:140])
    out.summary = (
        "Reached TextPeak on both endpoints."
        if out.ok
        else "TextPeak refused at least one endpoint; see below."
    )
    return out


async def _test_alerts(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Deliver a test alert down whichever channels are configured.

    This one really sends. There is no way to prove a bot token reaches the
    right chat without a message arriving, so the button says "Send", and the
    message says it is a test and who asked for it.
    """
    from c2w.alerts import slack, telegram
    from c2w.alerts.base import Alert, Severity

    out = SectionTest(True, "")
    platform = await settings_service.get_str(
        session, "core.platform_name", brand_id=brand_id
    )
    alert = Alert(
        title=f"Test alert from {platform}",
        body=(
            f"Sent by {actor} from the Alerts settings page to check this "
            "channel works. Nothing is wrong."
        ),
        severity=Severity.INFO,
        brand_id=brand_id,
    )

    telegram_token = await settings_service.get_secret(
        session, "alerts.telegram_bot_token", brand_id=brand_id
    )
    slack_url = await settings_service.get_secret(
        session, "alerts.slack_webhook_url", brand_id=brand_id
    )
    if not telegram_token and not slack_url:
        return SectionTest(
            False,
            "Neither Telegram nor Slack is configured, so there is nowhere to send to.",
        )

    if telegram_token:
        sent = await telegram.send(session, alert)
        out.add("telegram", sent, "delivered" if sent else "refused",
                "" if sent else "Check the bot token and that the chat id is the "
                                "one the bot has been added to")
        out.ok = out.ok and sent
    if slack_url:
        sent = await slack.send(session, alert)
        out.add("slack", sent, "delivered" if sent else "refused",
                "" if sent else "Check the webhook address; a revoked webhook "
                                "answers with an error rather than silence")
        out.ok = out.ok and sent

    out.summary = (
        "Test alert delivered. Check the channel."
        if out.ok
        else "At least one channel refused the test alert."
    )
    return out


async def _test_directory(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Bind to the domain controller and read one person back."""
    from c2w.auth import directory

    out = SectionTest(True, "")
    if not await settings_service.get_bool(session, "ldap.enabled", brand_id=brand_id):
        return SectionTest(False, "Active Directory is switched off on this page.")

    config = await directory.load_config(session, brand_id=brand_id)
    if not config.configured:
        return SectionTest(
            False, "The domain controller address or the search base is missing."
        )
    out.add("address", True, config.server_uri)
    out.add("encrypted", config.uses_tls, "ldaps" if config.uses_tls else "plain ldap",
            "" if config.uses_tls else "An ldap:// bind sends the reading "
                                       "account's password in clear across your network")

    result = await directory.probe(config)
    if not result.ok:
        out.ok = False
        out.summary = result.error
        out.add("read a person", False, result.error)
        return out
    out.add("read a person", True, f"{len(result.entries)} found")
    out.summary = f"Bound to the directory and read {len(result.entries)} account(s)."
    return out


async def _test_turnstile(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Ask Cloudflare whether the Turnstile secret is one it recognises.

    Verified with a deliberately invalid response token: a wrong secret comes
    back `invalid-input-secret`, while a good secret comes back
    `invalid-input-response` -- so the error distinguishes the two without
    needing somebody to solve a challenge.
    """
    out = SectionTest(True, "")
    secret = await settings_service.get_secret(
        session, "turnstile.secret_key", brand_id=brand_id
    )
    site = await settings_service.get_str(session, "turnstile.site_key", brand_id=brand_id)
    if not secret:
        return SectionTest(False, "No Turnstile secret key is set.")
    out.add("site key", bool(site), site or "not set",
            "" if site else "The public half, which the sign-in page needs")

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            url, data={"secret": secret, "response": "c2w-settings-test"}
        )
    payload = response.json()
    codes = payload.get("error-codes") or []
    if "invalid-input-secret" in codes:
        out.ok = False
        out.summary = "Cloudflare does not recognise that secret key."
        out.add("secret key", False, "invalid-input-secret",
                "Copy it again from the Turnstile widget's settings")
        return out
    out.add("secret key", True, "accepted by Cloudflare")
    out.summary = (
        "Cloudflare accepted the secret key. It rejected the dummy challenge "
        "response, which is exactly what should happen."
    )

    tunnel = await settings_service.get_secret(session, "tunnel.token", brand_id=brand_id)
    if tunnel:
        out.add(
            "tunnel token", None, "stored, but nothing runs a tunnel yet",
            "The token is kept for when the tunnel is built; it does nothing today",
        )
    return out
