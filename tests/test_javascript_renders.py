"""Every script this console serves must actually parse.

This exists because of a bug that reached the running system and stayed there:
``tojson`` was registered as a bare ``json.dumps``, so Jinja's autoescaping
turned its quotes into ``&#34;`` inside a ``<script>``. That is a JavaScript
syntax error, and the browser discards the *whole block* -- which is why the
header clock sat at ``--:--:--`` and the account menu stopped closing, from one
filter registration. Nothing failed loudly: the page returned 200, the markup
looked right, and only the behaviour was gone.

A template is not tested by rendering it and checking the status code. These
tests render the templates, pull out the inline scripts, and parse them, which
is the only check that would have caught it.

The same class of bug has now happened twice here -- the other time a Jinja
filter returned a plain string where HTML was meant, and the page showed escaped
markup. Both are "the template rendered fine and the result was wrong".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import esprima
import pytest
from jinja2 import Environment, FileSystemLoader

from c2w.web import filters

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "c2w" / "web" / "templates"
STATIC = Path(__file__).resolve().parent.parent / "src" / "c2w" / "web" / "static"

#: Inline scripts, i.e. not <script src="...">.
INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


class _Obj(dict):
    """Stand-in for the objects templates reach into with dots."""

    __getattr__ = dict.get


def _context() -> dict:
    """Enough context to render the shell. Values are shaped, not realistic."""
    return {
        "request": _Obj(url=_Obj(path="/calls")),
        "user": _Obj(
            email="someone@example.com",
            display_name="Someone",
            auth_source="LOCAL",
            preferences={},
            id=1,
            totp_active=False,
            totp_secret_sealed=None,
        ),
        # A theme with a quote in it would break a naive filter; a zone with a
        # slash is the real value that did.
        "prefs": {"theme": "dark", "font_px": 20, "volume": 140},
        "font_stack": 'system-ui,-apple-system,"Segoe UI",Arial,sans-serif',
        "org_timezone": "America/Puerto_Rico",
        "role_here": _Obj(label="operator", value="OPERATOR"),
        "brands": [],
        "active_brand": _Obj(name="Go4Rex", id=1),
        "permissions": set(),
        "nav": [],
        "recovery_left": 8,
        "mfa_required": False,
        "font_choices": (("100", "Default"),),
        "theme_choices": (("auto", "Match the system"),),
        "users": [],
        "user_brands": [],
        "assignable_roles": [],
        "memberships": {},
        "directory": {"any": False, "entra": False, "google": False, "ldap": False},
        "min_password_length": 12,
    }


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    filters.register(env)
    return env


def _inline_scripts(html: str) -> list[str]:
    return [block for block in INLINE.findall(html) if block.strip()]


class TestStaticScripts:
    @pytest.mark.parametrize(
        "name", sorted(p.name for p in STATIC.glob("*.js")), ids=lambda n: n
    )
    def test_parses(self, name: str) -> None:
        esprima.parseScript((STATIC / name).read_text())


class TestRenderedInlineScripts:
    """The templates that carry inline script, rendered and parsed."""

    # `_users.html` rather than a page: the users view is a section of the
    # settings rail now, so its type-ahead script lives in the partial.
    @pytest.mark.parametrize("template", ["base.html", "_users.html"])
    def test_parses(self, template: str) -> None:
        html = _env().get_template(template).render(**_context())
        blocks = _inline_scripts(html)
        assert blocks, f"{template} was expected to carry an inline script"
        for index, block in enumerate(blocks, 1):
            try:
                esprima.parseScript(block)
            except Exception as exc:  # pragma: no cover - only on a regression
                escaped = [ln.strip() for ln in block.splitlines() if "&#" in ln]
                pytest.fail(
                    f"{template} inline script {index} does not parse: {exc}\n"
                    + ("HTML-escaped text inside a script: " + escaped[0] if escaped else "")
                )

    def test_the_bug_this_file_exists_for_would_be_caught(self) -> None:
        """Re-introduce the old filter registration and prove it is detected.

        Without this, the tests above pass just as happily against a template
        that has no scripts at all, and nobody would notice if the guard stopped
        guarding.
        """
        env = _env()
        env.filters["tojson"] = json.dumps      # exactly what was live
        html = env.get_template("base.html").render(**_context())

        failures = []
        for block in _inline_scripts(html):
            try:
                esprima.parseScript(block)
            except Exception:
                failures.append(block)
        assert failures, (
            "a bare json.dumps in a <script> should produce unparseable output; "
            "if this no longer holds, the templates stopped embedding values in "
            "script and these tests are no longer checking anything"
        )
        assert "&#34;" in failures[0]


class TestJsonFilters:
    """The two contexts need different escaping, and one filter cannot do both."""

    def test_tojson_is_safe_inside_a_script(self) -> None:
        env = _env()
        out = env.from_string("var x = {{ v | tojson }};").render(v="</script><script>x")
        esprima.parseScript(out)
        # The closing tag must not survive as markup, or the value could end the
        # script element and start one of its own.
        assert "</script>" not in out
        assert "\\u003c" in out

    def test_tojson_keeps_a_plain_string_usable(self) -> None:
        env = _env()
        out = env.from_string("var z = {{ v | tojson }};").render(v="America/Puerto_Rico")
        assert out == 'var z = "America/Puerto_Rico";'
        esprima.parseScript(out)

    def test_json_attr_is_escaped_for_an_attribute(self) -> None:
        """Jinja's tojson leaves ``"`` alone, which would break value="...".."""
        env = _env()
        out = env.from_string('value="{{ v | json_attr }}"').render(v={"a": 1})
        assert out == 'value="{&#34;a&#34;: 1}"'


class TestZoneLabel:
    """The clock's label. It used to print "PUERTO RICO", which is not a time."""

    @pytest.mark.parametrize(
        "zone,expected",
        [
            ("America/Puerto_Rico", "GMT-4"),
            ("UTC", "UTC"),
            ("Asia/Kolkata", "GMT+5:30"),
            ("Not/AZone", "AZone"),
        ],
    )
    def test_reads_as_an_offset(self, zone: str, expected: str) -> None:
        env = _env()
        assert env.from_string("{{ z | zone_label }}").render(z=zone) == expected


class TestRenderedInlineStyles:
    """The <style> block is escaped-value territory too.

    Autoescaping turned the quotes in a font stack -- `"Segoe UI"` -- into
    `&#34;`, which is invalid CSS, so choosing a typeface silently did nothing.
    Same failure as the `<script>` one this file was written for: the template
    renders, the page returns 200, and only the behaviour is missing.
    """

    STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)

    def test_no_escaped_entity_reaches_a_style_block(self) -> None:
        context = _context()
        context["font_stack"] = (
            'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif'
        )
        html = _env().get_template("base.html").render(**context)
        blocks = self.STYLE.findall(html)
        assert blocks, "base.html was expected to carry an inline style block"
        for block in blocks:
            assert "&#" not in block, f"HTML-escaped text inside <style>: {block.strip()[:120]}"
            assert "&quot;" not in block
            assert "&amp;" not in block

    def test_the_font_stack_arrives_intact(self) -> None:
        context = _context()
        context["font_stack"] = 'Georgia,"Times New Roman",Times,serif'
        html = _env().get_template("base.html").render(**context)
        block = "\n".join(self.STYLE.findall(html))
        assert '"Times New Roman"' in block

    def test_the_size_is_a_number_of_pixels(self) -> None:
        context = _context()
        context["prefs"] = {"font_px": 24}
        html = _env().get_template("base.html").render(**context)
        assert "font-size: 24px" in html
