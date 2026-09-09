"""The Turnstile gate at sign-in.

The keys were storable and verified against Cloudflare for a while, but nothing
checked a challenge -- so switching it on protected nothing. These tests pin
the two decisions that matter more than the happy path:

* an unverifiable check (Cloudflare down, a timeout) must **not** lock anybody
  out of the only route into the console;
* a check that is present and wrong must be refused.

Getting those the wrong way round is either a lockout during somebody else's
outage, or a gate that waves everything through.
"""

from __future__ import annotations

import httpx
import pytest

from c2w.auth import turnstile


def _gate(**kw):
    return turnstile.TurnstileGate(
        required=kw.pop("required", True),
        site_key=kw.pop("site_key", "0x-site"),
        secret_key=kw.pop("secret_key", "0x-secret"),
        **kw,
    )


class TestWhetherToChallenge:
    def test_the_widget_hides_when_a_key_is_missing(self):
        """Rendering it without a secret would produce a check nothing verifies."""
        assert _gate(secret_key="").show_widget is False
        assert _gate(site_key="").show_widget is False
        assert _gate().show_widget is True

    def test_a_gate_that_is_not_required_shows_nothing(self):
        assert _gate(required=False).show_widget is False

    @pytest.mark.parametrize(
        "ip,private",
        [
            ("192.168.21.5", True),
            ("10.1.2.3", True),
            ("172.16.9.9", True),
            ("145.239.102.215", False),
            ("8.8.8.8", False),
            (None, False),
            ("not-an-ip", False),
        ],
    )
    def test_private_addresses_are_recognised(self, ip, private):
        """`skip_on_lan` depends on this, and a wrong answer either exempts the
        internet or challenges the office."""
        assert turnstile._is_private(ip) is private


class TestVerifying:
    async def test_a_gate_that_is_off_always_passes(self):
        assert await turnstile.verify(_gate(required=False), "") is True

    async def test_no_token_is_refused(self):
        """The one case a bot produces: posting the form without the widget."""
        assert await turnstile.verify(_gate(), "") is False

    async def test_cloudflare_accepting_passes(self, monkeypatch):
        monkeypatch.setattr(
            httpx.AsyncClient, "post",
            _reply({"success": True}),
        )
        assert await turnstile.verify(_gate(), "token") is True

    async def test_cloudflare_rejecting_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            httpx.AsyncClient, "post",
            _reply({"success": False, "error-codes": ["invalid-input-response"]}),
        )
        assert await turnstile.verify(_gate(), "token") is False

    async def test_an_outage_does_not_lock_anybody_out(self, monkeypatch):
        """Deliberate, and the more important of the two directions.

        A widget that blocks the only route into the console when a third party
        has an outage trades a little bot protection for a total loss of
        access. The password and the second factor are still underneath.
        """
        async def boom(self, *a, **kw):
            raise httpx.ConnectError("cloudflare unreachable")

        monkeypatch.setattr(httpx.AsyncClient, "post", boom)
        assert await turnstile.verify(_gate(), "token") is True

    async def test_a_timeout_does_not_lock_anybody_out(self, monkeypatch):
        async def slow(self, *a, **kw):
            raise httpx.ReadTimeout("too slow")

        monkeypatch.setattr(httpx.AsyncClient, "post", slow)
        assert await turnstile.verify(_gate(), "token") is True

    async def test_a_private_client_address_is_not_sent_to_cloudflare(self, monkeypatch):
        """`remoteip` is only meaningful for a routable address."""
        seen: dict = {}

        async def capture(self, url, data=None, **kw):
            seen.update(data or {})
            return httpx.Response(200, json={"success": True})

        monkeypatch.setattr(httpx.AsyncClient, "post", capture)
        await turnstile.verify(_gate(), "token", client_ip="192.168.21.5")
        assert "remoteip" not in seen
        await turnstile.verify(_gate(), "token", client_ip="145.239.102.215")
        assert seen["remoteip"] == "145.239.102.215"


def _reply(payload: dict):
    async def post(self, url, data=None, **kw):
        return httpx.Response(200, json=payload)

    return post


class TestSectionTestRegistry:
    """One button per thing a card can reach.

    The Cloudflare card configures two unrelated things, and a single button
    labelled "Check the Turnstile keys" that quietly also tested the tunnel
    meant nobody knew the tunnel had been tested. These pin the split so it
    cannot silently collapse back.
    """

    def test_cloudflare_offers_both_checks(self):
        from c2w.web.settings_tests import tests_for

        keys = [t["key"] for t in tests_for("Cloudflare")]
        assert keys == ["turnstile", "tunnel"]
        labels = [t["label"] for t in tests_for("Cloudflare")]
        assert any("Turnstile" in label for label in labels)
        assert any("tunnel" in label for label in labels)

    def test_every_label_says_what_it_tests(self):
        """A label that under-describes its button is how this went wrong."""
        from c2w.web.settings_tests import TESTABLE_SECTIONS

        for category, checks in TESTABLE_SECTIONS.items():
            keys = [k for k, _ in checks]
            assert len(keys) == len(set(keys)), f"{category} has a duplicate check key"
            for key, label in checks:
                assert label.strip(), f"{category}/{key} has no label"

    def test_a_card_with_nothing_to_reach_offers_no_button(self):
        from c2w.web.settings_tests import tests_for

        assert tests_for("Retention") == []
        assert tests_for("Your company") == []

    async def test_every_registered_check_has_a_runner(self):
        """A button that dispatches to nothing would report "nothing to test"."""
        from c2w.web.settings_tests import TESTABLE_SECTIONS, run_section_test

        class _NoSettings:
            async def execute(self, *a, **kw):  # pragma: no cover - not reached
                raise AssertionError("should not query")

        for category, checks in TESTABLE_SECTIONS.items():
            for key, _ in checks:
                out = await run_section_test(
                    _NoSettings(), category, brand_id=None, actor="t", check=key
                )
                # Each runner is reached and fails on the stub session rather
                # than returning the "nothing to test" sentinel.
                assert "nothing to test" not in out.summary.lower(), f"{category}/{key}"

    async def test_an_unknown_check_falls_back_to_the_first(self):
        """An older link, or a form without the field, must still do something."""
        from c2w.web.settings_tests import run_section_test

        class _NoSettings:
            async def execute(self, *a, **kw):  # pragma: no cover
                raise AssertionError("should not query")

        out = await run_section_test(
            _NoSettings(), "Cloudflare", brand_id=None, actor="t", check="nonsense"
        )
        assert "nothing to test" not in out.summary.lower()
