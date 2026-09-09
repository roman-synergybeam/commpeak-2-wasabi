"""Cloudflare Turnstile at the sign-in page.

The keys have been storable for a while and were verified against Cloudflare;
nothing checked a challenge, so switching it on protected nothing. This closes
that.

Two decisions worth stating:

* **It never locks anybody out.** If the challenge cannot be *verified* --
  Cloudflare unreachable, a timeout, keys removed while somebody was mid-login
  -- the sign-in proceeds and the failure is logged loudly. A widget that
  blocks the only route into the console when a third party has an outage
  trades a small amount of bot protection for a total loss of access. The
  password and the second factor are still doing their job underneath.
  A challenge that is *present and wrong* is refused, which is the case that
  matters.
* **The local network can be exempted.** `turnstile.skip_on_lan` is on by
  default: the widget needs outbound internet from the *browser*, and an
  operator on the office LAN reaching this console by its private address
  should not be stopped by that.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.settings import settings_service

__all__ = ["TurnstileGate", "load_gate", "verify"]

log = get_logger(__name__)

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
#: Short: this sits in front of a sign-in, and a slow third party must not make
#: signing in feel broken.
TIMEOUT_SECONDS = 6.0
#: The field Turnstile's widget posts.
FIELD = "cf-turnstile-response"


@dataclass(frozen=True, slots=True)
class TurnstileGate:
    """Whether this sign-in has to solve a challenge, and with which key."""

    required: bool
    site_key: str = ""
    secret_key: str = ""
    reason: str = ""

    @property
    def show_widget(self) -> bool:
        """The widget renders only when it could actually be checked."""
        return self.required and bool(self.site_key and self.secret_key)


def _is_private(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


async def load_gate(session: AsyncSession, *, client_ip: str | None) -> TurnstileGate:
    """Decide whether to challenge this visitor.

    Global settings, not per-brand: the sign-in page is reached before anybody
    knows which organisation the visitor belongs to.
    """
    if not await settings_service.get_bool(session, "turnstile.enabled"):
        return TurnstileGate(False, reason="switched off")

    site = (await settings_service.get_str(session, "turnstile.site_key") or "").strip()
    secret = await settings_service.get_secret(session, "turnstile.secret_key")
    if not site or not secret:
        # Switched on but unusable. Loudly, because somebody believes this is
        # protecting the sign-in page.
        log.warning(
            "turnstile.misconfigured",
            detail="enabled without both keys; the challenge is not being applied",
        )
        return TurnstileGate(False, reason="keys missing")

    if await settings_service.get_bool(session, "turnstile.skip_on_lan") and _is_private(
        client_ip
    ):
        return TurnstileGate(False, site_key=site, secret_key=secret, reason="local network")

    return TurnstileGate(True, site_key=site, secret_key=secret)


async def verify(gate: TurnstileGate, token: str, *, client_ip: str | None = None) -> bool:
    """Check one challenge response. True when the sign-in may proceed.

    Returns True on an *unverifiable* failure -- see the module docstring: a
    Cloudflare outage must not be a lockout. Returns False only when
    Cloudflare positively rejects the token, or when no token was sent at all.
    """
    if not gate.required:
        return True
    if not token:
        log.info("turnstile.missing_response")
        return False

    payload = {"secret": gate.secret_key, "response": token}
    if client_ip and not _is_private(client_ip):
        payload["remoteip"] = client_ip
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(VERIFY_URL, data=payload)
        body = response.json()
    except Exception as exc:
        # Fail open, loudly. The password and second factor still apply.
        log.warning("turnstile.unverifiable", error=str(exc)[:200], decision="allowed")
        return True

    if body.get("success"):
        return True
    codes = body.get("error-codes") or []
    log.info("turnstile.rejected", codes=codes)
    return False
