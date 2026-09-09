"""Signing in with Microsoft Entra ID or Google Workspace.

The authorization-code flow with PKCE, written out rather than handed to a
framework helper, because the parts that matter here are the checks -- and a
helper that silently skips one is worse than code you can read.

What is verified on the way back, and why each one matters:

* **state**, against a value held in a signed short-lived cookie. Without it,
  anyone can hand somebody a callback URL and log them into an account of the
  attacker's choosing.
* **nonce**, against the same cookie. This is what ties the ID token to *this*
  sign-in attempt; a token replayed from elsewhere fails here.
* **PKCE verifier**. The authorization code is useless to anybody who
  intercepts it without the verifier, which never leaves this server.
* **signature**, against the provider's published JWKS, fetched over TLS and
  cached briefly. An unsigned or wrongly-signed token is the whole attack.
* **issuer and audience**. A token minted for a *different* application, or by
  a different tenant, is a valid token -- just not for us.
* **expiry**, with a small leeway for clock skew.
* **email verification and the domain allow list**, because "signed in with
  Google" is not the same as "works for this company" -- anyone with a Gmail
  address can complete the flow otherwise.

Nothing here trusts a claim before the signature is checked.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.config import get_bootstrap
from c2w.logging import get_logger
from c2w.settings import settings_service

__all__ = [
    "COOKIE",
    "OidcError",
    "Provider",
    "ProviderConfig",
    "authorize_url",
    "complete",
    "load_provider",
]

log = get_logger(__name__)

#: The short-lived cookie holding state, nonce and the PKCE verifier. A cookie
#: rather than a server-side row: it is single-use, expires in minutes, and
#: putting it in the database would mean a table to clean up for no benefit.
COOKIE = "c2w_oidc"
FLOW_MAX_AGE_SECONDS = 600
#: Clock skew allowance when checking `exp`/`iat`. Small: this is a signed
#: assertion, not a long-lived credential.
LEEWAY_SECONDS = 60
JWKS_CACHE_SECONDS = 3600
HTTP_TIMEOUT = 15.0


class OidcError(Exception):
    """A sign-in attempt failed, with a message safe to show."""


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    #: Where the discovery document lives, once the tenant is known.
    discovery: str
    scopes: str = "openid email profile"

    @property
    def enabled_setting(self) -> str:
        return f"auth.oidc_{self.key}_enabled"


ENTRA = Provider(
    key="entra",
    label="Microsoft",
    discovery="https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
)
GOOGLE = Provider(
    key="google",
    label="Google",
    discovery="https://accounts.google.com/.well-known/openid-configuration",
)
PROVIDERS: dict[str, Provider] = {ENTRA.key: ENTRA, GOOGLE.key: GOOGLE}


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: Provider
    client_id: str
    client_secret: str
    discovery_url: str
    allowed_domains: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass
class Identity:
    """A verified identity. Every field here came from a checked token."""

    subject: str
    email: str
    name: str = ""
    groups: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


async def load_provider(session: AsyncSession, key: str) -> ProviderConfig:
    """Read one provider's settings. Raises if it is not usable."""
    provider = PROVIDERS.get(key)
    if provider is None:
        raise OidcError("unknown sign-in provider")
    if not await settings_service.get_bool(session, provider.enabled_setting):
        raise OidcError(f"{provider.label} sign-in is switched off")

    client_id = (
        await settings_service.get_str(session, f"auth.oidc_{key}_client_id") or ""
    ).strip()
    secret = await settings_service.get_secret(session, f"auth.oidc_{key}_client_secret")
    if provider is ENTRA:
        tenant = (
            await settings_service.get_str(session, "auth.oidc_entra_tenant_id") or ""
        ).strip()
        if not tenant:
            raise OidcError("the Microsoft directory (tenant) ID is not set")
        discovery = provider.discovery.format(tenant=tenant)
        domains_key = "auth.entra_allowed_domains"
    else:
        discovery = provider.discovery
        domains_key = "auth.google_allowed_domains"

    raw_domains = await settings_service.get_str(session, domains_key) or ""
    domains = tuple(
        d.strip().lower().lstrip("@") for d in raw_domains.replace(";", ",").split(",") if d.strip()
    )

    config = ProviderConfig(provider, client_id, secret, discovery, domains)
    if not config.configured:
        raise OidcError(f"{provider.label} sign-in is missing its client ID or secret")
    return config


def _signer() -> URLSafeTimedSerializer:
    key = get_bootstrap().master_key.get_secret_value()
    if not key:
        raise OidcError("the platform master key is not configured")
    return URLSafeTimedSerializer(key, salt="c2w-oidc-flow")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def _discover(config: ProviderConfig) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(config.discovery_url)
        response.raise_for_status()
        return response.json()


_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


async def _jwks(uri: str) -> dict[str, Any]:
    """The provider's signing keys, cached briefly.

    Cached because a key fetch per sign-in is a needless dependency on someone
    else's uptime; briefly, because providers rotate keys and a stale cache
    would reject every token until a restart.
    """
    now = time.time()
    hit = _JWKS_CACHE.get(uri)
    if hit and now - hit[0] < JWKS_CACHE_SECONDS:
        return hit[1]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(uri)
        response.raise_for_status()
        keys = response.json()
    _JWKS_CACHE[uri] = (now, keys)
    return keys


async def authorize_url(config: ProviderConfig, redirect_uri: str) -> tuple[str, str]:
    """Where to send the browser, and the cookie value to remember it by."""
    meta = await _discover(config)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())

    params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": config.provider.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if config.provider is GOOGLE and config.allowed_domains:
        # A hint, never a control: Google honours it for the account chooser
        # but the domain is still verified on the way back.
        params["hd"] = config.allowed_domains[0]
    if config.provider is ENTRA:
        params["response_mode"] = "query"

    query = "&".join(f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items())
    url = f"{meta['authorization_endpoint']}?{query}"
    cookie = _signer().dumps(
        {
            "provider": config.provider.key,
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
        }
    )
    return url, cookie


def _decode_flow(cookie: str) -> dict[str, Any]:
    try:
        data = _signer().loads(cookie, max_age=FLOW_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise OidcError("that sign-in took too long; please start again") from exc
    except BadSignature as exc:
        raise OidcError("that sign-in could not be verified; please start again") from exc
    if not isinstance(data, dict):
        raise OidcError("that sign-in could not be verified; please start again")
    return data


async def complete(
    config: ProviderConfig, *, code: str, state: str, cookie: str
) -> Identity:
    """Finish the flow and return a verified identity."""
    flow = _decode_flow(cookie)
    if flow.get("provider") != config.provider.key:
        raise OidcError("that sign-in was started with a different provider")
    # Constant-time, because this is a secret comparison like any other.
    if not secrets.compare_digest(str(flow.get("state") or ""), state or ""):
        raise OidcError("that sign-in could not be verified; please start again")

    meta = await _discover(config)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": flow["redirect_uri"],
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": flow["verifier"],
            },
            headers={"Accept": "application/json"},
        )
    if response.status_code >= 400:
        log.warning(
            "oidc.token_exchange_failed",
            provider=config.provider.key,
            status=response.status_code,
            body=response.text[:200],
        )
        raise OidcError("the provider refused to complete the sign-in")

    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise OidcError("the provider did not return an identity token")

    claims = await _verify_id_token(
        id_token,
        config=config,
        meta=meta,
        expected_nonce=str(flow.get("nonce") or ""),
    )
    return _identity_from(claims, config)


async def _verify_id_token(
    id_token: str, *, config: ProviderConfig, meta: dict[str, Any], expected_nonce: str
) -> dict[str, Any]:
    """Check the signature and every claim that decides who this is."""
    from authlib.jose import JsonWebToken

    keys = await _jwks(meta["jwks_uri"])
    # The algorithms the provider advertises, minus `none` -- an unsigned token
    # is the whole attack, and a permissive list is how it gets accepted.
    allowed = [
        a for a in meta.get("id_token_signing_alg_values_supported", ["RS256"])
        if a.lower() != "none"
    ] or ["RS256"]
    try:
        claims = JsonWebToken(allowed).decode(id_token, key=keys)
    except Exception as exc:
        log.warning("oidc.bad_signature", provider=config.provider.key, error=str(exc)[:160])
        raise OidcError("the identity token could not be verified") from exc

    now = time.time()
    if str(claims.get("aud")) != config.client_id and config.client_id not in (
        claims.get("aud") or []
    ):
        # A perfectly valid token, minted for somebody else's application.
        raise OidcError("that identity token was issued for a different application")

    issuer = str(claims.get("iss") or "")
    expected_issuer = str(meta.get("issuer") or "")
    if config.provider is ENTRA:
        # Entra's issuer carries the tenant id, and the discovery document for
        # a single tenant states it exactly.
        if issuer != expected_issuer:
            raise OidcError("that identity token came from a different directory")
    elif issuer.rstrip("/") not in {
        expected_issuer.rstrip("/"),
        "https://accounts.google.com",
        "accounts.google.com",
    }:
        raise OidcError("that identity token came from an unexpected issuer")

    if float(claims.get("exp", 0)) + LEEWAY_SECONDS < now:
        raise OidcError("that sign-in has expired; please start again")
    if float(claims.get("iat", now)) - LEEWAY_SECONDS > now:
        raise OidcError("that identity token is not valid yet")

    nonce = str(claims.get("nonce") or "")
    if not expected_nonce or not secrets.compare_digest(nonce, expected_nonce):
        # This is what ties the token to *this* attempt.
        raise OidcError("that sign-in could not be matched; please start again")

    return dict(claims)


def _identity_from(claims: dict[str, Any], config: ProviderConfig) -> Identity:
    """Turn verified claims into an identity, enforcing the domain rules."""
    email = str(
        claims.get("email")
        or claims.get("preferred_username")
        or claims.get("upn")
        or ""
    ).strip().lower()
    if not email or "@" not in email:
        raise OidcError("the provider did not return an email address")

    # Google says whether it verified the address; Microsoft does not, and a
    # work account's address is verified by the directory itself.
    if config.provider is GOOGLE and claims.get("email_verified") is False:
        raise OidcError("that Google address is not verified")

    domain = email.rsplit("@", 1)[1]
    if config.allowed_domains and domain not in config.allowed_domains:
        # Without this, anybody with a personal account at the provider can
        # complete the flow. "Signed in with Google" is not "works here".
        log.info("oidc.domain_refused", provider=config.provider.key, domain=domain)
        raise OidcError(f"{domain} is not allowed to sign in to this system")

    groups = claims.get("groups") or claims.get("roles") or []
    if isinstance(groups, str):
        groups = [groups]

    return Identity(
        subject=str(claims.get("sub") or email),
        email=email,
        name=str(claims.get("name") or "").strip(),
        groups=[str(g) for g in groups],
        raw={k: v for k, v in claims.items() if k not in {"at_hash", "nonce"}},
    )


def flow_cookie_kwargs(*, secure: bool) -> dict[str, Any]:
    """How the flow cookie must be set.

    `samesite=lax`, not `strict`: the provider redirects the browser back with
    a top-level GET, and `strict` would withhold the cookie on exactly that
    request, breaking every sign-in.
    """
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "max_age": FLOW_MAX_AGE_SECONDS,
        "path": "/",
    }


def state_debug(cookie: str) -> str:
    """The state a flow cookie carries, for tests and diagnostics only."""
    return str(_decode_flow(cookie).get("state") or "")
