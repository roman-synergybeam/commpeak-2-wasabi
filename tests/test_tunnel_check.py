"""The Cloudflare tunnel check, and the false negative it used to report.

This file exists because of a specific wrong answer. The check used to look up
the public DNS for the tunnel's hostname and expect a CNAME pointing at
``<tunnel-id>.cfargotunnel.com``. That record is never publicly visible: a
tunnel route is always *proxied*, so Cloudflare answers with its own anycast A
records. The check therefore said "c2w.it-saas.com does not point into this
tunnel" about a hostname that was serving the console perfectly -- and it
returned early on that verdict, so the end-to-end fetch that would have shown
the truth never ran.

The tests below pin both halves of the fix: DNS is not consulted at all, and
the fetch is what decides.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

import pytest

from c2w.web import settings_tests
from c2w.web.settings_tests import SectionTest


class _Reply:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """Stands in for httpx.AsyncClient, recording what was asked for."""

    requested: ClassVar[list[str]] = []
    reply: ClassVar[_Reply] = _Reply(200, {"status": "ok", "version": "0.1.0"})

    def __init__(self, *_a: object, **_k: object) -> None:
        pass

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def get(self, url: str, **_k: object) -> _Reply:
        _Client.requested.append(url)
        return _Client.reply


@pytest.fixture(autouse=True)
def _stub(monkeypatch: pytest.MonkeyPatch) -> None:
    _Client.requested = []
    _Client.reply = _Reply(200, {"status": "ok", "version": "0.1.0"})
    monkeypatch.setattr(settings_tests.httpx, "AsyncClient", _Client)

    async def get_bool(*_a: object, **_k: object) -> bool:
        return True

    async def get_secret(*_a: object, **_k: object) -> str:
        # A connector token is base64 JSON; only the "t" field is read.
        return "eyJ0IjoiMTExMTExMTEtMjIyMi0zMzMzLTQ0NDQtNTU1NTU1NTU1NTU1In0"

    async def get_str(*_a: object, **_k: object) -> str:
        return "c2w.example.com"

    monkeypatch.setattr(settings_tests.settings_service, "get_bool", get_bool)
    monkeypatch.setattr(settings_tests.settings_service, "get_secret", get_secret)
    monkeypatch.setattr(settings_tests.settings_service, "get_str", get_str)

    async def status() -> dict[str, object]:
        return {"readyConnections": 4}

    monkeypatch.setattr(settings_tests, "_tunnel_status", status)


def _names(out: SectionTest) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in out.checks}


async def _run(monkeypatch: pytest.MonkeyPatch, *, counter: list[int | None]) -> SectionTest:
    seq = iter(counter)

    async def requests() -> int | None:
        return next(seq)

    monkeypatch.setattr(settings_tests, "_tunnel_requests", requests)
    out = SectionTest(True, "")
    await settings_tests._check_tunnel(None, None, out, [])  # type: ignore[arg-type]
    return out


async def test_proxied_hostname_with_no_cname_is_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact case that used to fail: working tunnel, no public CNAME."""
    out = await _run(monkeypatch, counter=[10, 11])
    names = _names(out)
    assert out.ok, out.checks
    assert names["answers on the public address"]["ok"] is True
    assert names["routed through this tunnel"]["ok"] is True
    # The old wording must not come back.
    assert not any("does not point into this tunnel" in str(c) for c in out.checks)


async def test_the_fetch_is_never_gated_behind_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with no usable request counter, the end-to-end fetch still runs."""
    out = await _run(monkeypatch, counter=[None, None])
    assert "https://c2w.example.com/api/health" in _Client.requested
    assert _names(out)["answers on the public address"]["ok"] is True
    assert out.ok


async def test_a_stranger_answering_the_hostname_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some other server on that name returns 200 without our health shape."""
    _Client.reply = _Reply(200, {"nginx": "hello"})
    out = await _run(monkeypatch, counter=[10, 10])
    assert not out.ok
    assert _names(out)["answers on the public address"]["ok"] is False


async def test_a_still_counter_is_reported_but_does_not_fail_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering, but not through this cloudflared: worth saying, not an error.

    The hostname demonstrably serves the console, so calling this a failure
    would repeat the original mistake in the other direction.
    """
    out = await _run(monkeypatch, counter=[10, 10])
    assert out.ok
    assert _names(out)["routed through this tunnel"]["ok"] is None


def test_the_module_no_longer_infers_routing_from_dns() -> None:
    """A guard on the premise itself, not just on this one code path.

    Docstrings and comments are stripped first, because the fixed code
    deliberately *explains* the cfargotunnel mistake and a naive substring
    search over the whole file would trip on that explanation -- and would
    then have to be loosened until it no longer guarded anything.
    """
    source = Path("src/c2w/web/settings_tests.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)

    for banned in ("cfargotunnel", "dns.google", "_hostname_routed"):
        assert banned not in code, f"{banned} is back in executable code"
