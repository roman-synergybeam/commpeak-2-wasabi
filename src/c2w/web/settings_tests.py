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

import contextlib
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.settings import settings_service

__all__ = ["TESTABLE_SECTIONS", "SectionTest", "run_section_test", "tests_for"]

log = get_logger(__name__)

#: Which cards get buttons, and what each button says and does.
#:
#: A list per card rather than one test each, because a card can configure two
#: unrelated things. Cloudflare is the case in point: the Turnstile keys and
#: the tunnel are set up separately, fail separately, and are fixed in
#: different places -- and a single button labelled "Check the Turnstile keys"
#: that quietly also tested the tunnel meant nobody knew the tunnel had been
#: tested at all.
#:
#: A section absent from here has nothing outside this box to talk to, so a
#: test could only tell you what the form already shows.
TESTABLE_SECTIONS: dict[str, list[tuple[str, str]]] = {
    "CommPeak calls": [("cdr", "Test the call records connection")],
    "CommPeak messages": [("sms", "Test the messages connection")],
    "Alerts": [("alerts", "Send a test alert")],
    "Active Directory": [("directory", "Test the directory connection")],
    "Cloudflare": [
        ("turnstile", "Test the Turnstile keys"),
        ("tunnel", "Test the tunnel"),
    ],
}


def tests_for(category: str) -> list[dict[str, str]]:
    """The buttons a settings card should show."""
    return [
        {"key": key, "label": label}
        for key, label in TESTABLE_SECTIONS.get(category, [])
    ]


@dataclass
class SectionTest:
    """What a section's test found. Never raises past the route."""

    ok: bool
    summary: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str = "", hint: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})


async def run_section_test(
    session: AsyncSession,
    category: str,
    *,
    brand_id: int | None,
    actor: str,
    check: str = "",
) -> SectionTest:
    """Run one named check. ``check`` picks which, when a card has several."""
    runners = {
        "cdr": _test_cdr_api,
        "sms": _test_textpeak,
        "alerts": _test_alerts,
        "directory": _test_directory,
        "turnstile": _test_turnstile,
        "tunnel": _test_tunnel,
    }
    available = [key for key, _ in TESTABLE_SECTIONS.get(category, [])]
    if not available:
        return SectionTest(False, "There is nothing to test on this page.")
    # An unnamed check runs the card's first, so an older link or a form
    # without the field still does something sensible.
    wanted = check if check in available else available[0]
    runner = runners.get(wanted)
    if runner is None:  # pragma: no cover - registry and runners are in step
        return SectionTest(False, "There is nothing to test on this page.")
    try:
        return await runner(session, brand_id, actor)
    except Exception as exc:  # every failure is a message, not a 500
        log.warning(
            "settings.test_failed", category=category, check=wanted, error=str(exc)[:200]
        )
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
    """The "are you human" check at sign-in: are the keys real, and is it on?

    Its own button, separate from the tunnel. They are configured on the same
    card because both are Cloudflare, but they fail separately and are fixed in
    different places -- and one button that did both meant nobody knew the
    tunnel had been tested.
    """
    out = SectionTest(True, "")

    secret = await settings_service.get_secret(
        session, "turnstile.secret_key", brand_id=brand_id
    )
    site = await settings_service.get_str(session, "turnstile.site_key", brand_id=brand_id)
    if not secret:
        out.add("turnstile", None, "no secret key set",
                "The sign-in challenge is off until both keys are here")
    else:
        out.add("turnstile site key", bool(site), site or "not set",
                "" if site else "The public half, which the sign-in page needs")
        # Verified with a deliberately invalid response token: a wrong secret
        # answers `invalid-input-secret`, a good one `invalid-input-response`,
        # so the error tells them apart without anybody solving a challenge.
        async with httpx.AsyncClient(timeout=10) as client:
            reply = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": "c2w-settings-test"},
            )
        codes = (reply.json() or {}).get("error-codes") or []
        if "invalid-input-secret" in codes:
            out.ok = False
            out.add("turnstile secret key", False, "Cloudflare does not recognise it",
                    "Copy it again from the Turnstile widget's settings")
        else:
            out.add("turnstile secret key", True, "accepted by Cloudflare")
            enforced = await settings_service.get_bool(
                session, "turnstile.enabled", brand_id=brand_id
            )
            out.add(
                "challenge enforced at sign-in", enforced or None,
                "on" if enforced else "off",
                "" if enforced else "The keys work; switch it on above to use them",
            )

    if not out.summary:
        if not secret:
            out.summary = (
                "No Turnstile secret key is set, so the sign-in challenge is off."
            )
        else:
            out.summary = (
                "Cloudflare accepted the secret key. It rejected the dummy challenge "
                "response, which is exactly what should happen."
                if out.ok
                else "Turnstile is not usable as configured; see below."
            )
    return out


async def _test_tunnel(
    session: AsyncSession, brand_id: int | None, actor: str
) -> SectionTest:
    """Is the tunnel configured, connected, routed, and reachable?

    Four separate answers because they fail separately, and the commonest
    outcome by far is "connected but nothing routed to it": a connector token
    authorises the daemon to join the tunnel and cannot create the DNS record,
    so the hostname keeps pointing wherever it did before.
    """
    out = SectionTest(True, "")
    notes: list[str] = []
    await _check_tunnel(session, brand_id, out, notes)
    if not out.summary:
        out.summary = " ".join(notes) or (
            "The tunnel is up." if out.ok
            else "The tunnel is not carrying traffic yet; see below."
        )
    return out


#: cloudflared serves its own status on the first free port in this range when
#: none is given. Probed rather than assumed, because the port shifts if
#: something else already holds one.
_CLOUDFLARED_PORTS = (20241, 20242, 20243, 20244, 20245)


async def _tunnel_status() -> dict[str, Any] | None:
    """What the local cloudflared says about itself, or None if none is running.

    Asked of the daemon rather than of systemd: a unit can be `active` while
    the tunnel has no connections, and "four connections to Cloudflare's edge"
    is the fact worth reporting.
    """
    async with httpx.AsyncClient(timeout=3) as client:
        for port in _CLOUDFLARED_PORTS:
            # Nothing listening is the normal case for four of the five, so
            # the miss is suppressed rather than logged five times a click.
            reply = None
            with contextlib.suppress(Exception):
                reply = await client.get(f"http://127.0.0.1:{port}/ready")
            if reply is not None and reply.status_code < 500:
                with contextlib.suppress(Exception):
                    return dict(reply.json())
    return None


async def _check_tunnel(
    session: AsyncSession,
    brand_id: int | None,
    out: SectionTest,
    notes: list[str],
) -> None:
    """The tunnel half: is it configured, is it connected, is it routed?

    Three separate answers because they fail separately, and the most common
    outcome by far is "connected but nothing routed to it" -- a connector
    token authorises the daemon to join the tunnel and cannot create the DNS
    record, so the hostname keeps pointing wherever it did before.
    """
    enabled = await settings_service.get_bool(session, "tunnel.enabled", brand_id=brand_id)
    token = await settings_service.get_secret(session, "tunnel.token", brand_id=brand_id)
    hostname = (
        await settings_service.get_str(session, "tunnel.hostname", brand_id=brand_id) or ""
    ).strip()

    if not enabled and not token:
        out.add("tunnel", None, "not configured",
                "Only needed to reach this console from outside your network")
        return
    if not token:
        out.ok = False
        out.add("tunnel token", False, "not set",
                "Create a tunnel in Cloudflare Zero Trust and paste its connector token")
        return

    tunnel_id = _tunnel_id(token)
    out.add("tunnel token", True, f"tunnel {tunnel_id[:8]}…" if tunnel_id else "stored")

    status = await _tunnel_status()
    if status is None:
        out.ok = False
        out.add(
            "tunnel running", False, "no cloudflared on this server is answering",
            "Start it with: systemctl --user start c2w-tunnel",
        )
        return
    ready = int(status.get("readyConnections") or 0)
    out.add(
        "tunnel connected", ready > 0,
        f"{ready} connection(s) to Cloudflare's edge",
        "" if ready else "The daemon is running but has not registered; check its log",
    )
    if not ready:
        out.ok = False

    if not hostname:
        out.add("public address", None, "not set",
                "Set it here and add the same hostname to the tunnel in Cloudflare")
        return

    routed = await _hostname_routed(hostname, tunnel_id)
    if routed is True:
        out.add("public address", True, f"{hostname} points into this tunnel")
    elif routed is False:
        out.ok = False
        out.add(
            "public address", False, f"{hostname} does not point into this tunnel",
            "A connector token cannot create the DNS record. In Cloudflare Zero "
            "Trust open this tunnel, add a Public Hostname for it, and point it "
            "at http://localhost:8000",
        )
        return
    else:
        out.add("public address", None, f"{hostname} could not be resolved from here")

    # The only check that proves the whole path: out through Cloudflare and
    # back into this process.
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            reply = await client.get(f"https://{hostname}/api/health")
        reached = reply.status_code < 500
        out.add(
            "reachable from outside", reached, f"HTTP {reply.status_code}",
            "" if reached else "Cloudflare answered but could not reach this server; "
                               "check the tunnel's service address is http://localhost:8000",
        )
        if reached:
            notes.append(f"The console is reachable at https://{hostname}.")
        else:
            out.ok = False
    except Exception as exc:
        out.ok = False
        out.add("reachable from outside", False, str(exc)[:120])


def _tunnel_id(token: str) -> str:
    """The tunnel id a connector token carries.

    Decoded locally so the check can say *which* tunnel is configured, and so
    the DNS comparison below has something to compare against. The token is
    base64 JSON; a token that will not decode is worth saying early rather
    than after starting a daemon that can only fail.
    """
    import base64
    import json

    with contextlib.suppress(Exception):
        body = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
        return str(body.get("t") or "")
    return ""


async def _hostname_routed(hostname: str, tunnel_id: str) -> bool | None:
    """Whether the hostname is a CNAME into this tunnel.

    True, False, or None when it cannot be resolved at all. Uses a public
    resolver over HTTPS because the answer wanted is what the *internet* sees,
    which a split-horizon resolver on the LAN may not give.
    """
    if not tunnel_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            reply = await client.get(
                "https://dns.google/resolve",
                params={"name": hostname, "type": "CNAME"},
            )
        answers = (reply.json() or {}).get("Answer") or []
    except Exception:
        return None
    if not answers:
        return False
    return any(f"{tunnel_id}.cfargotunnel.com" in str(a.get("data", "")) for a in answers)
