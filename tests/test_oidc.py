"""Signing in with Microsoft or Google.

There is no test tenant here, so these do the next best and more useful thing:
mint ID tokens with a locally generated RSA key, serve them through a stubbed
discovery document and JWKS, and check that each verification actually refuses
what it should. Every one of these tests is a way in if the check is missing.

What is deliberately *not* covered, and needs a real tenant: the provider's own
consent screen, and whether the redirect address registered there matches.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt

from c2w.auth import oidc

CLIENT_ID = "c2w-test-client"
ISSUER = "https://login.microsoftonline.com/tenant-abc/v2.0"

_KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)
_PUBLIC_JWKS = {"keys": [json.loads(_KEY.as_json(is_private=False))]}


def _config(**kw) -> oidc.ProviderConfig:
    return oidc.ProviderConfig(
        provider=kw.pop("provider", oidc.ENTRA),
        client_id=kw.pop("client_id", CLIENT_ID),
        client_secret="secret",
        discovery_url="https://example.invalid/.well-known/openid-configuration",
        allowed_domains=kw.pop("allowed_domains", ()),
    )


def _meta(issuer: str = ISSUER) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


def _id_token(**claims) -> str:
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "subject-1",
        "email": "ana@corp.example",
        "name": "Ana Ruiz",
        "nonce": "the-nonce",
        "iat": now,
        "exp": now + 300,
    }
    body.update(claims)
    return jwt.encode({"alg": "RS256"}, body, _KEY).decode()


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    """Discovery and JWKS served locally; no network in these tests."""
    async def discover(config):
        return _meta()

    async def jwks(uri):
        return _PUBLIC_JWKS

    monkeypatch.setattr(oidc, "_discover", discover)
    monkeypatch.setattr(oidc, "_jwks", jwks)
    oidc._JWKS_CACHE.clear()
    monkeypatch.setenv("C2W_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    import c2w.config

    c2w.config.get_bootstrap.cache_clear()
    yield
    c2w.config.get_bootstrap.cache_clear()


def _flow_cookie(**overrides) -> str:
    data = {
        "provider": "entra",
        "state": "the-state",
        "nonce": "the-nonce",
        "verifier": "the-verifier",
        "redirect_uri": "https://console.example/auth/entra/callback",
    }
    data.update(overrides)
    return oidc._signer().dumps(data)


def _token_reply(monkeypatch, payload: dict, status: int = 200):
    async def post(self, url, data=None, **kw):
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)


class TestStartingTheFlow:
    async def test_the_url_carries_pkce_state_and_nonce(self):
        url, cookie = await oidc.authorize_url(
            _config(), "https://console.example/auth/entra/callback"
        )
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "response_type=code" in url
        assert f"client_id={CLIENT_ID}" in url
        # The verifier itself must never appear in the URL -- that is the whole
        # point of PKCE.
        state = oidc.state_debug(cookie)
        assert state and f"state={state}" in url
        assert "code_verifier" not in url

    async def test_each_start_is_unique(self):
        """A reused state or nonce would make replay possible."""
        first, _ = await oidc.authorize_url(_config(), "https://c/cb")
        second, _ = await oidc.authorize_url(_config(), "https://c/cb")
        assert first != second


class TestCompletingTheFlow:
    async def test_a_good_token_yields_an_identity(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token()})
        identity = await oidc.complete(
            _config(), code="c", state="the-state", cookie=_flow_cookie()
        )
        assert identity.email == "ana@corp.example"
        assert identity.subject == "subject-1"
        assert identity.name == "Ana Ruiz"

    async def test_a_mismatched_state_is_refused(self, monkeypatch):
        """Without this, a crafted callback logs somebody into another account."""
        _token_reply(monkeypatch, {"id_token": _id_token()})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="not-the-state", cookie=_flow_cookie()
            )

    async def test_a_mismatched_nonce_is_refused(self, monkeypatch):
        """The nonce is what ties the token to *this* attempt."""
        _token_reply(monkeypatch, {"id_token": _id_token(nonce="somebody-elses")})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_a_token_for_another_application_is_refused(self, monkeypatch):
        """A perfectly valid token, minted for someone else's client id."""
        _token_reply(monkeypatch, {"id_token": _id_token(aud="another-app")})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_a_token_from_another_issuer_is_refused(self, monkeypatch):
        _token_reply(
            monkeypatch,
            {"id_token": _id_token(iss="https://login.microsoftonline.com/other/v2.0")},
        )
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_an_expired_token_is_refused(self, monkeypatch):
        past = int(time.time()) - 3600
        _token_reply(monkeypatch, {"id_token": _id_token(exp=past, iat=past - 300)})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_a_token_signed_by_the_wrong_key_is_refused(self, monkeypatch):
        other = JsonWebKey.generate_key("RSA", 2048, is_private=True)
        now = int(time.time())
        forged = jwt.encode(
            {"alg": "RS256"},
            {
                "iss": ISSUER, "aud": CLIENT_ID, "sub": "attacker",
                "email": "attacker@corp.example", "nonce": "the-nonce",
                "iat": now, "exp": now + 300,
            },
            other,
        ).decode()
        _token_reply(monkeypatch, {"id_token": forged})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_a_tampered_flow_cookie_is_refused(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token()})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state",
                cookie=_flow_cookie()[:-4] + "AAAA",
            )

    async def test_a_flow_started_with_another_provider_is_refused(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token()})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state",
                cookie=_flow_cookie(provider="google"),
            )

    async def test_a_refused_token_exchange_is_reported(self, monkeypatch):
        _token_reply(monkeypatch, {"error": "invalid_grant"}, status=400)
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )

    async def test_no_id_token_is_refused(self, monkeypatch):
        _token_reply(monkeypatch, {"access_token": "only-this"})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )


class TestDomainRules:
    """"Signed in with Google" is not "works for this company"."""

    async def test_an_allowed_domain_passes(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token()})
        identity = await oidc.complete(
            _config(allowed_domains=("corp.example",)),
            code="c", state="the-state", cookie=_flow_cookie(),
        )
        assert identity.email.endswith("@corp.example")

    async def test_a_domain_not_on_the_list_is_refused(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token(email="someone@gmail.com")})
        with pytest.raises(oidc.OidcError, match="not allowed"):
            await oidc.complete(
                _config(allowed_domains=("corp.example",)),
                code="c", state="the-state", cookie=_flow_cookie(),
            )

    async def test_an_unverified_google_address_is_refused(self, monkeypatch):
        _token_reply(
            monkeypatch,
            {"id_token": _id_token(email_verified=False, iss="https://accounts.google.com")},
        )
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(provider=oidc.GOOGLE),
                code="c", state="the-state",
                cookie=_flow_cookie(provider="google"),
            )

    async def test_a_token_with_no_address_is_refused(self, monkeypatch):
        _token_reply(monkeypatch, {"id_token": _id_token(email=None)})
        with pytest.raises(oidc.OidcError):
            await oidc.complete(
                _config(), code="c", state="the-state", cookie=_flow_cookie()
            )


class TestCookieHandling:
    def test_the_flow_cookie_is_lax_not_strict(self):
        """`strict` withholds the cookie on the provider's redirect back,
        which would break every sign-in."""
        kwargs = oidc.flow_cookie_kwargs(secure=True)
        assert kwargs["samesite"] == "lax"
        assert kwargs["httponly"] is True
        assert kwargs["secure"] is True
        assert kwargs["max_age"] <= 900
