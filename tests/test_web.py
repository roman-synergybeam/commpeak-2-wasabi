"""HTTP-level tests for the UI and media endpoints.

Driven through the real ASGI app against a real database, because the things
worth testing here are the ones that only appear end to end: that an
unauthenticated browser is redirected rather than shown JSON, that a role
without `recordings.download` is refused, and that RLS still applies when the
request arrives over HTTP.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.auth.local import create_super_admin, hash_password
from c2w.db.models.auth import AuthSource, Role, User

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)


@pytest.fixture
async def app_client(monkeypatch):
    """The real app, wired to the scratch database."""
    monkeypatch.setenv("C2W_DATABASE_URL", TEST_DB)
    import c2w.config
    import c2w.db.session as session_mod

    c2w.config.get_bootstrap.cache_clear()
    await session_mod.dispose_engine()

    from c2w.api.app import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await session_mod.dispose_engine()


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def scenario(db):
    """A brand with one call and one playable-looking recording, plus users."""
    slug = f"web-{uuid.uuid4().hex[:8]}"
    async with db() as s:
        brand_id = (
            await s.execute(
                text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
                {"n": slug, "s": slug},
            )
        ).scalar_one()
        for table in ("cdrs", "recordings"):
            await s.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand_id} "
                    f"PARTITION OF {table} FOR VALUES IN ({brand_id})"
                )
            )
        await s.commit()

        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (brand_id, name, slug) "
                    "VALUES (:b, 'td', :s) RETURNING id"
                ),
                {"b": brand_id, "s": slug},
            )
        ).scalar_one()
        conn_id = (
            await s.execute(
                text(
                    "INSERT INTO commpeak_connections "
                    "(brand_id, tenant_id, name, s3_bucket, s3_access_key_sealed, "
                    " s3_secret_sealed) "
                    "VALUES (:b, :t, 'TD', :bucket, 'x', 'x') RETURNING id"
                ),
                {"b": brand_id, "t": tenant_id, "bucket": slug},
            )
        ).scalar_one()

        start = datetime.now(UTC) - timedelta(hours=2)
        call_uuid = str(uuid.uuid4())
        cdr_id = (
            await s.execute(
                text(
                    "INSERT INTO cdrs (brand_id, connection_id, tenant_id, call_uuid, "
                    "start_at, end_at, call_duration, direction, src, dst, src_norm, "
                    "dst_norm, status) VALUES (:b, :c, :t, :u, :st, :en, 42, 'in', "
                    "'441632960770@did.commpeak.com', '0007281', '632960770', '7281', "
                    "'NORMAL_CLEARING') RETURNING id"
                ),
                {
                    "b": brand_id, "c": conn_id, "t": tenant_id, "u": call_uuid,
                    "st": start, "en": start + timedelta(seconds=42),
                },
            )
        ).scalar_one()
        rec_id = (
            await s.execute(
                text(
                    "INSERT INTO recordings (brand_id, connection_id, tenant_id, source_key, "
                    "source_size, started_at, cdr_id, call_uuid, match_method, "
                    "match_confidence, state, file_ext, direction, number) "
                    "VALUES (:b, :c, :t, :k, 1234, :st, :cdr, :u, 'epoch_exact', 0.99, "
                    "'AVAILABLE', 'flac', 'in', '441632960770') RETURNING id"
                ),
                {
                    "b": brand_id, "c": conn_id, "t": tenant_id, "u": call_uuid,
                    "k": "2026/09/08/00/in-441632960770-101-20260908-000000-1757292737.0.flac",
                    "st": start, "cdr": cdr_id,
                },
            )
        ).scalar_one()

        admin_email = f"admin-{slug}@example.com"
        admin = await create_super_admin(s, admin_email, "a-long-enough-password")
        agent_email = f"agent-{slug}@example.com"
        s.add(
            User(
                brand_id=brand_id,
                email=agent_email,
                display_name="Agent",
                role=Role.OPERATOR,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password("a-long-enough-password"),
            )
        )
        await s.commit()

    return {
        "brand_id": brand_id,
        "cdr_id": cdr_id,
        "recording_id": rec_id,
        "admin_email": admin_email,
        "agent_email": agent_email,
        "password": "a-long-enough-password",
        "admin_id": admin.id,
    }


async def _login(
    client: AsyncClient, email: str, password: str, *, brand_id: int | None = None
):
    response = await client.post(
        "/login", data={"email": email, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    if brand_id is not None:
        # A super admin is not scoped to a brand, so they land on the first one
        # and switch from the sidebar. Tests pick the brand explicitly, the same
        # way a real super admin would.
        client.cookies.set("c2w_brand", str(brand_id))
    return response


class TestAuthFlow:
    async def test_login_page_renders(self, app_client):
        response = await app_client.get("/login")
        assert response.status_code == 200
        assert "Sign in" in response.text

    async def test_browser_is_redirected_not_shown_json(self, app_client):
        """An unauthenticated page request goes to the sign-in page."""
        response = await app_client.get(
            "/", headers={"accept": "text/html"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    async def test_api_client_gets_401(self, app_client):
        """A non-browser caller gets a status code, not a redirect."""
        response = await app_client.get(
            "/api/v1/recordings/1/stream", headers={"accept": "application/json"}
        )
        assert response.status_code == 401

    async def test_wrong_password_is_rejected(self, app_client, scenario):
        response = await app_client.post(
            "/login",
            data={"email": scenario["admin_email"], "password": "wrong-password-here"},
        )
        assert response.status_code == 401
        assert "invalid email or password" in response.text

    async def test_login_then_dashboard(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/")
        assert response.status_code == 200
        assert "Dashboard" in response.text

    async def test_logout_revokes_the_session(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        await app_client.get("/logout", follow_redirects=False)
        response = await app_client.get(
            "/", headers={"accept": "text/html"}, follow_redirects=False
        )
        assert response.status_code == 303


class TestCallsUi:
    async def test_calls_list_shows_the_call(self, app_client, scenario):
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        response = await app_client.get("/calls")
        assert response.status_code == 200
        assert "441632960770" in response.text
        assert f'/calls/{scenario["cdr_id"]}' in response.text

    async def test_media_pill_renders_as_html_not_escaped(self, app_client, scenario):
        """A filter returning HTML must return Markup, or the table shows tags."""
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        response = await app_client.get("/calls")
        assert '<span class="pill ok">playable</span>' in response.text
        assert "&lt;span" not in response.text

    async def test_number_search_filters(self, app_client, scenario):
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        hit = await app_client.get("/calls/rows", params={"number": "441632960770"})
        miss = await app_client.get("/calls/rows", params={"number": "999888777"})
        assert f'/calls/{scenario["cdr_id"]}' in hit.text
        assert f'/calls/{scenario["cdr_id"]}' not in miss.text

    async def test_media_filter_none_excludes_calls_with_recordings(self, app_client, scenario):
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        response = await app_client.get("/calls/rows", params={"media": "none"})
        assert f'/calls/{scenario["cdr_id"]}' not in response.text

    async def test_duration_filter_is_applied(self, app_client, scenario):
        """This filter existed on the query object but was not wired to the
        route, so it silently did nothing."""
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        included = await app_client.get("/calls/rows", params={"min_duration": 10})
        excluded = await app_client.get("/calls/rows", params={"min_duration": 600})
        assert f'/calls/{scenario["cdr_id"]}' in included.text
        assert f'/calls/{scenario["cdr_id"]}' not in excluded.text

    async def test_call_detail_shows_correlation_and_player(self, app_client, scenario):
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        response = await app_client.get(f"/calls/{scenario['cdr_id']}")
        assert response.status_code == 200
        # The correlation tier is shown as a human label, not as the stored
        # machine value: the database says epoch_exact, the operator reads
        # "matched exactly".
        assert "matched exactly" in response.text
        assert "epoch_exact" not in response.text
        assert f"/api/v1/recordings/{scenario['recording_id']}/stream" in response.text

    async def test_csv_export(self, app_client, scenario):
        await _login(
            app_client, scenario["admin_email"], scenario["password"],
            brand_id=scenario["brand_id"],
        )
        response = await app_client.get("/calls/export.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "started_at,ended_at,direction" in response.text
        assert "441632960770@did.commpeak.com" in response.text


class TestPermissions:
    async def test_operator_may_listen_and_export(self, app_client, scenario):
        """An operator's job is to find a call, hear it and take a copy."""
        await _login(app_client, scenario["agent_email"], scenario["password"])
        detail = await app_client.get(f"/calls/{scenario['cdr_id']}")
        assert detail.status_code == 200
        assert f"/api/v1/recordings/{scenario['recording_id']}/stream" in detail.text
        assert f"/api/v1/recordings/{scenario['recording_id']}/download" in detail.text

        allowed = await app_client.get(
            f"/api/v1/recordings/{scenario['recording_id']}/download",
            follow_redirects=False,
        )
        # 409 rather than 403: the role permits it, this recording has no
        # archive copy in the fixture.
        assert allowed.status_code in (307, 409)

    async def test_operator_cannot_reach_settings(self, app_client, scenario):
        await _login(app_client, scenario["agent_email"], scenario["password"])
        response = await app_client.get("/admin/settings")
        assert response.status_code == 403

    async def test_operator_cannot_reach_the_audit_log(self, app_client, scenario):
        await _login(app_client, scenario["agent_email"], scenario["password"])
        assert (await app_client.get("/audit")).status_code == 403

    async def test_admin_can_reach_settings(self, app_client, scenario):
        """The landing view is the setup checklist, with the rail beside it."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/admin/settings")
        assert response.status_code == 200
        assert 'class="railnav"' in response.text
        assert "/admin/settings?section=retention" in response.text

    async def test_every_section_renders_and_holds_its_own_settings(
        self, app_client, scenario
    ):
        """Each section is its own view; a setting appears in exactly one.

        Guards the reason the rail exists: if a category were dropped from the
        groups it would become unreachable from the UI while still existing in
        the registry, and nothing else would notice.
        """
        from c2w.auth.rbac import permissions_for
        from c2w.db.models.auth import Role
        from c2w.settings_spec import specs_by_category
        from c2w.web.routes import _section_slug, _settings_sections

        await _login(app_client, scenario["admin_email"], scenario["password"])
        categories = specs_by_category()
        # As a platform administrator, so the two permission-gated sections are
        # included and the assertion below still covers every category.
        reachable = {
            item["name"]
            for group in _settings_sections(categories, permissions_for(Role.SUPER_ADMIN))
            for item in group["sections"]
        }
        assert set(categories) <= reachable, set(categories) - reachable

        for name, specs in categories.items():
            response = await app_client.get(
                f"/admin/settings?section={_section_slug(name)}"
            )
            assert response.status_code == 200, name
            # Its own settings are here...
            assert f'name="set:{specs[0].key}"' in response.text, name
            # ...and it is the only card on the page. Counted by the save
            # form, not by `name="category"`: the test button posts its own
            # form with the same field, so that count is two on any section
            # that has something to test.
            assert response.text.count('action="/admin/settings">') == 1, name

    async def test_an_unknown_section_falls_back_to_the_checklist(
        self, app_client, scenario
    ):
        """A stale bookmark should show something useful, not a 404."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/admin/settings?section=no-such-thing")
        assert response.status_code == 200
        assert 'class="steps"' in response.text

    async def test_no_section_ever_renders_a_secret(self, app_client, scenario, db):
        """Every section is swept, not just the one holding the token.

        Stronger than the single-page check this replaces: splitting the page
        up means a leak could hide on any one of eighteen views, so all of
        them are checked.
        """
        from c2w.settings import settings_service
        from c2w.settings_spec import SETTINGS, specs_by_category
        from c2w.web.routes import _section_slug

        secret_keys = [k for k, spec in SETTINGS.items() if spec.sensitive]
        assert secret_keys, "no sensitive settings to check"
        async with db() as s:
            for index, key in enumerate(secret_keys):
                await settings_service.set(s, key, f"leak-canary-{index}")
            await s.commit()
        settings_service.invalidate()

        await _login(app_client, scenario["admin_email"], scenario["password"])
        for name in specs_by_category():
            response = await app_client.get(
                f"/admin/settings?section={_section_slug(name)}"
            )
            assert "leak-canary" not in response.text, f"a secret leaked into {name}"

        # And the section that holds one still says that it is set, with a tail
        # short enough to check against the console it came from.
        telegram = await app_client.get(
            f"/admin/settings?section={_section_slug(SETTINGS['alerts.telegram_bot_token'].category)}"
        )
        assert "stored, ending" in telegram.text


class TestBrandIsolationOverHttp:
    async def test_a_user_cannot_switch_to_another_brand(self, app_client, scenario, db):
        """The brand cookie is user input; it must be validated, not trusted."""
        async with db() as s:
            other = (
                await s.execute(
                    text("INSERT INTO brands (name, slug) VALUES (:n, :n) RETURNING id"),
                    {"n": f"other-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            await s.commit()

        await _login(app_client, scenario["agent_email"], scenario["password"])
        response = await app_client.post(
            "/switch-brand", data={"brand_id": other}, follow_redirects=False
        )
        assert response.status_code == 403

    async def test_forged_brand_cookie_does_not_expose_another_brand(
        self, app_client, scenario, db
    ):
        """An ordinary user is pinned to their own brand regardless of cookies."""
        async with db() as s:
            other = (
                await s.execute(
                    text("INSERT INTO brands (name, slug) VALUES (:n, :n) RETURNING id"),
                    {"n": f"forge-{uuid.uuid4().hex[:8]}"},
                )
            ).scalar_one()
            await s.commit()

        await _login(app_client, scenario["agent_email"], scenario["password"])
        app_client.cookies.set("c2w_brand", str(other))
        response = await app_client.get("/calls")
        assert response.status_code == 200
        # Still their own brand's data, because the cookie is ignored for
        # non-super-admins.
        assert "441632960770" in response.text


class TestOps:
    async def test_health_needs_no_auth_and_no_database(self, app_client):
        response = await app_client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_ready_reports_the_database(self, app_client):
        response = await app_client.get("/api/ready")
        assert response.status_code == 200
        assert response.json()["checks"]["database"] == "ok"

    async def test_metrics_served(self, app_client):
        response = await app_client.get("/api/metrics")
        assert response.status_code == 200
        assert "python_gc_objects_collected_total" in response.text


class TestOrganisationsAndUsersMovedIntoSettings:
    """Both were pages in the top menu and are now sections of the rail.

    The move is only correct if it changed where they are and nothing else --
    in particular not who can reach them. `/admin/organisations` was guarded
    by `brands.manage` and `/admin/users` by `users.manage`, and the settings
    page is guarded by `settings.view`, so rendering them inside it is a
    chance to widen access by accident.
    """

    async def test_both_sections_are_in_the_rail_for_a_platform_admin(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings")
        assert page.status_code == 200
        assert "/admin/settings?section=organisations" in page.text
        assert "/admin/settings?section=users" in page.text

    async def test_the_organisations_pane_renders_its_own_content(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?section=organisations")
        assert page.status_code == 200
        # The create form, and no settings card -- this section has no specs.
        assert 'action="/admin/organisations"' in page.text
        assert 'action="/admin/settings">' not in page.text

    async def test_the_users_pane_renders_its_own_content(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?section=users")
        assert page.status_code == 200
        assert 'action="/admin/users"' in page.text
        assert scenario["agent_email"] in page.text

    async def test_the_old_addresses_forward_to_the_sections(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        for old, slug in (
            ("/admin/organisations", "organisations"),
            ("/admin/users", "users"),
        ):
            moved = await app_client.get(old, follow_redirects=False)
            assert moved.status_code == 303, old
            assert moved.headers["location"] == f"/admin/settings?section={slug}"

    async def test_a_saved_message_survives_the_forward(self, app_client, scenario):
        """The dozen POST handlers still redirect to the old address."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        moved = await app_client.get(
            "/admin/users?saved=someone%40example.com", follow_redirects=False
        )
        assert moved.status_code == 303
        assert "saved=someone%40example.com" in moved.headers["location"]
        assert "section=users" in moved.headers["location"]

    async def test_an_organisation_admin_sees_users_but_not_organisations(
        self, app_client, scenario, db
    ):
        """`brands.manage` is the platform administrator's alone.

        An organisation admin has every other permission, so this is the one
        section the rail must leave out for them -- and asking for it directly
        must not render it either.
        """
        email = f"orgadmin-{uuid.uuid4().hex[:8]}@example.com"
        async with db() as s:
            s.add(
                User(
                    brand_id=scenario["brand_id"],
                    email=email,
                    display_name="Org admin",
                    role=Role.ADMIN,
                    auth_source=AuthSource.LOCAL,
                    password_hash=hash_password("a-long-enough-password"),
                )
            )
            await s.commit()

        await _login(app_client, email, "a-long-enough-password")
        rail = await app_client.get("/admin/settings")
        assert "/admin/settings?section=users" in rail.text
        assert "/admin/settings?section=organisations" not in rail.text

        # Asked for by name, it falls back to the checklist rather than
        # rendering a pane this role may not see.
        direct = await app_client.get("/admin/settings?section=organisations")
        assert direct.status_code == 200
        assert 'action="/admin/organisations"' not in direct.text
        assert 'class="steps"' in direct.text

    async def test_neither_is_in_the_top_menu_any_more(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings")
        nav = page.text.split('<nav class="top">', 1)[1].split("</nav>", 1)[0]
        assert "/admin/organisations" not in nav
        assert "/admin/users" not in nav


class TestTheProbeCooldown:
    """Testing an account twice in a row must not reach CommPeak twice.

    CommPeak rate-limits, and its rate-limited 403 is indistinguishable from
    the 403 an address that is not on the account's access list gets. Eight
    accounts all showing red is an invitation to click Test repeatedly, and
    doing so manufactures the exact failure the page is meant to diagnose --
    that is how an address which had listed a bucket successfully came to be
    refused for the next hour. So the second click is refused locally.
    """

    async def test_a_second_probe_within_the_window_sends_nothing(self, db, scenario):
        from datetime import UTC, datetime

        from c2w.web import accounts

        sent: list[int] = []

        async def _explode(session, conn):
            sent.append(conn.id)
            raise AssertionError("the cooldown should have stopped this")

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            conn_id = (
                await s.execute(
                    text(
                        "SELECT id FROM commpeak_connections WHERE brand_id = :b "
                        "ORDER BY id LIMIT 1"
                    ),
                    {"b": scenario["brand_id"]},
                )
            ).scalar_one()
            await s.execute(
                text(
                    "UPDATE commpeak_connections SET last_probe_at = :now WHERE id = :i"
                ),
                {"now": datetime.now(UTC), "i": conn_id},
            )
            await s.commit()

        original = accounts.open_source
        accounts.open_source = _explode
        try:
            async with db() as s:
                await s.execute(
                    text("SELECT set_config('c2w.brand_id', :b, false)"),
                    {"b": str(scenario["brand_id"])},
                )
                outcome = await accounts.test_connection(
                    s, scenario["brand_id"], conn_id
                )
        finally:
            accounts.open_source = original

        assert sent == [], "a request went to CommPeak inside the cooldown"
        assert not outcome.ok
        assert "Wait" in outcome.summary
        # Reported as a note, not as a failure of the account itself.
        assert outcome.checks[0]["ok"] is None

    async def test_the_window_is_short_enough_to_stay_usable(self):
        """Long enough to stop a burst, short enough not to be in the way."""
        from c2w.web.accounts import PROBE_COOLDOWN_SECONDS

        assert 5 <= PROBE_COOLDOWN_SECONDS <= 60


class TestShowingStoredCredentials:
    """"stored" is not something an operator can check.

    With eight accounts and a refusal that names none of them, a token typed
    into the wrong account is invisible: every field just says "stored". So
    there is a button that shows what is actually held. It is gated by the same
    permission as saving, on the reasoning that whoever can *overwrite* both
    values loses nothing by seeing them -- but unlike saving it is written to
    the change log, because it is the one action that takes a credential out of
    a sealed column and puts it on a screen.
    """

    TOKEN = "PROBEONLYTOKEN123456"
    SECRET = "probe-only-secret-value-not-real-0000000"

    async def _make(self, db, brand_id):
        from c2w.web.accounts import add_connection

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(brand_id)},
            )
            conn = await add_connection(
                s,
                brand_id,
                {
                    "name": f"reveal-{uuid.uuid4().hex[:8]}",
                    "commpeak_domain": "reveal.test.commpeak.com",
                    "s3_bucket": str(uuid.uuid4()),
                    "s3_access_key": self.TOKEN,
                    "s3_secret": self.SECRET,
                },
                actor="test@example.com",
            )
            await s.commit()
            return conn.id

    async def test_it_returns_exactly_what_was_stored(self, db, scenario):
        """A round trip through the sealed columns, not a mock."""
        from c2w.web.accounts import reveal_credentials

        conn_id = await self._make(db, scenario["brand_id"])
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            _name, token, secret = await reveal_credentials(
                s, scenario["brand_id"], conn_id
            )
        assert token == self.TOKEN
        assert secret == self.SECRET

    async def test_another_organisation_cannot_reveal_it(self, db, scenario):
        """The brand is part of the lookup, not just of the RLS scope."""
        from c2w.web.accounts import AccountError, reveal_credentials

        conn_id = await self._make(db, scenario["brand_id"])
        other = scenario["brand_id"] + 1000
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(other)},
            )
            with pytest.raises(AccountError):
                await reveal_credentials(s, other, conn_id)

    async def test_an_operator_is_refused(self, app_client, scenario, db):
        conn_id = await self._make(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["agent_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        refused = await app_client.post(
            "/admin/connections",
            data={"action": "reveal", "connection_id": str(conn_id)},
            follow_redirects=False,
        )
        assert refused.status_code == 403

    async def test_it_is_logged_without_the_values(self, app_client, scenario, db):
        """The row says somebody looked. It must not say what they saw."""
        conn_id = await self._make(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["admin_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        done = await app_client.post(
            "/admin/connections",
            data={"action": "reveal", "connection_id": str(conn_id)},
            follow_redirects=False,
        )
        assert done.status_code == 303

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            rows = (
                await s.execute(
                    text(
                        "SELECT action, result, detail::text FROM audit_events "
                        "WHERE action = 'CREDENTIALS_REVEALED' ORDER BY id DESC LIMIT 1"
                    )
                )
            ).all()
        assert rows, "the reveal was not written to the change log"
        _action, result, detail = rows[0]
        assert result == "SUCCESS"
        assert self.TOKEN not in detail
        assert self.SECRET not in detail

    async def test_it_is_shown_once_and_not_on_a_refresh(
        self, app_client, scenario, db
    ):
        """Popped by the render, so reloading does not repeat it."""
        conn_id = await self._make(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["admin_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        await app_client.post(
            "/admin/connections",
            data={"action": "reveal", "connection_id": str(conn_id)},
            follow_redirects=False,
        )
        first = await app_client.get(f"/admin/connections?tested={conn_id}")
        assert self.TOKEN in first.text
        assert self.SECRET in first.text

        again = await app_client.get(f"/admin/connections?tested={conn_id}")
        assert self.TOKEN not in again.text
        assert self.SECRET not in again.text

    async def test_the_page_never_shows_a_credential_unasked(
        self, app_client, scenario, db
    ):
        """Without pressing the button, nothing is on the page."""
        conn_id = await self._make(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["admin_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        page = await app_client.get("/admin/connections")
        assert self.TOKEN not in page.text
        assert self.SECRET not in page.text
        section = await app_client.get("/admin/settings?section=commpeak-calls")
        assert self.TOKEN not in section.text
        assert self.SECRET not in section.text
        assert str(conn_id) in page.text          # the account itself is listed


class TestTheOrganisationsPaneIgnoresTheSelectedBrand:
    """A platform administrator asking to see the organisations means all of them.

    The pane runs on the ordinary request session, which carries forced RLS
    scoped to whichever brand the profile has selected. So it showed the
    selected organisation correctly and every other one as zeros with no
    PBXes -- which reads as "that company has nothing in it" rather than "you
    are looking at the other one". Wrong numbers presented confidently are
    worse than an error.
    """

    async def test_every_organisation_shows_its_own_real_figures(self, db, scenario):
        from c2w.web.routes import _organisations_pane

        other_slug = f"other-{uuid.uuid4().hex[:8]}"
        async with db() as s:
            other_id = (
                await s.execute(
                    text(
                        "INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"
                    ),
                    {"n": f"Other {other_slug}", "s": other_slug},
                )
            ).scalar_one()
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(other_id)}
            )
            await s.execute(
                text(
                    "INSERT INTO tenants (brand_id, name, slug) VALUES (:b, 'other.pbx', :s)"
                ),
                {"b": other_id, "s": f"t-{uuid.uuid4().hex[:6]}"},
            )
            await s.commit()

        # Scoped to the *scenario* brand, as a request would be.
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            pane = await _organisations_pane(s)

            ids = {b.id for b in pane["organisations"]}
            assert {scenario["brand_id"], other_id} <= ids

            # The other organisation's PBX is counted and listed, even though
            # the session is scoped elsewhere. This is the regression.
            assert pane["counts"][other_id]["tenants"] >= 1, pane["counts"][other_id]
            assert any(
                t["name"] == "other.pbx" for t in pane["tenants"].get(other_id, [])
            ), pane["tenants"].get(other_id)

            # And the selected brand is still right.
            assert pane["counts"][scenario["brand_id"]]["tenants"] >= 1

    async def test_it_restores_the_scope_it_was_given(self, db, scenario):
        """The caller goes on to read settings for the brand actually chosen.

        Moving the scope per organisation and forgetting to put it back would
        leave the rest of the page reading whichever organisation happened to
        sort last.
        """
        from c2w.web.routes import _organisations_pane

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            await _organisations_pane(s)
            still = (
                await s.execute(text("SELECT current_setting('c2w.brand_id', true)"))
            ).scalar_one()
        assert still == str(scenario["brand_id"])

    async def test_membership_in_another_organisation_is_counted(self, db, scenario):
        """`user_brands` is how a person works across organisations.

        Counting only `users.brand_id` missed every such person, so an
        organisation with five shared users read as empty.
        """
        from c2w.web.routes import _organisations_pane

        email = f"shared-{uuid.uuid4().hex[:8]}@example.com"
        async with db() as s:
            # Their *home* organisation is a different one -- which is the
            # whole point: `users.brand_id` will never count them here, only
            # the `user_brands` membership will. A non-super-admin must have a
            # home brand (ck_users_brand_required), so this is also the only
            # shape the constraint allows.
            home_id = (
                await s.execute(
                    text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
                    {
                        "n": f"Home {uuid.uuid4().hex[:6]}",
                        "s": f"home-{uuid.uuid4().hex[:8]}",
                    },
                )
            ).scalar_one()
            uid = (
                await s.execute(
                    text(
                        "INSERT INTO users (brand_id, email, display_name, role, "
                        "auth_source, password_hash) "
                        "VALUES (:h, :e, 'Shared', 'OPERATOR', 'LOCAL', :p) "
                        "RETURNING id"
                    ),
                    {
                        "h": home_id,
                        "e": email,
                        "p": hash_password("a-long-enough-password"),
                    },
                )
            ).scalar_one()
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            await s.execute(
                text(
                    "INSERT INTO user_brands (user_id, brand_id, role) "
                    "VALUES (:u, :b, 'OPERATOR')"
                ),
                {"u": uid, "b": scenario["brand_id"]},
            )
            await s.commit()

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            pane = await _organisations_pane(s)
        assert pane["counts"][scenario["brand_id"]]["people"] >= 1


class TestForeverIsOfferedForArchiveRetention:
    """`0` years means forever, and must not render as "0"."""

    def test_zero_is_a_choice_and_reads_as_forever(self):
        from c2w.settings_spec import SETTINGS

        spec = SETTINGS["retention.keep_archive_years"]
        assert "0" in spec.choices
        assert spec.choice_labels.get("0") == "forever"

    def test_zero_passes_validation(self):
        """A validator rejecting 0 would make the option unselectable."""
        from c2w.settings_spec import SETTINGS

        spec = SETTINGS["retention.keep_archive_years"]
        assert spec.validator is not None
        spec.validator(0)

    def test_no_code_turns_the_number_into_a_deletion_cutoff(self):
        """The danger `0` carries, guarded rather than trusted.

        Nothing deletes from the archive on age today. If something starts to,
        it must treat 0 as "never" -- because the obvious implementation,
        `now - years`, makes 0 mean "delete everything immediately", which is
        the exact opposite of what the operator picked.
        """
        from pathlib import Path

        for path in Path("src/c2w").rglob("*.py"):
            if path.name == "settings_spec.py":
                continue
            body = path.read_text()
            if "keep_archive_years" in body:
                assert "forever" in body or "== 0" in body or "or None" in body, (
                    f"{path} consumes keep_archive_years -- it must handle 0 as "
                    "forever, and say so"
                )

    async def test_the_menu_renders_the_label(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?section=retention")
        assert page.status_code == 200
        assert '<option value="0"' in page.text
        assert "forever" in page.text


class TestSettingsSearch:
    """112 settings in 17 sections is more than the rail alone can serve.

    The rail only helps if you already know which section a thing is filed
    under. Somebody looking for "telegram" should not have to guess "Alerts".
    """

    async def test_it_finds_a_setting_by_its_name(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?q=telegram")
        assert page.status_code == 200
        assert "alerts.telegram_chat_id" in page.text
        # And links to the section it lives in.
        assert "section=alerts" in page.text

    async def test_it_finds_a_setting_by_its_dotted_key(self, app_client, scenario):
        """A log line or a support message names the key, not the label."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?q=source.key_root_prefix")
        assert "Where recordings start in the bucket" in page.text

    async def test_a_single_character_is_not_a_search(self, app_client, scenario):
        """It would match most of the registry: the whole list, with steps."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?q=a")
        assert "at least two characters" in page.text

    async def test_no_match_says_what_it_searched(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?q=zzzznotathing")
        assert "Nothing matched" in page.text

    async def test_a_label_match_outranks_a_description_mention(self):
        from c2w.auth.rbac import permissions_for
        from c2w.db.models.auth import Role
        from c2w.web.routes import _search_settings

        found = _search_settings("retention", permissions_for(Role.SUPER_ADMIN))
        assert found, "expected matches for 'retention'"
        # The first hit must be something actually named for it, not a setting
        # that merely mentions it in prose.
        assert "retention" in found[0]["label"].lower() or found[0]["key"].startswith(
            "retention."
        ), found[0]

    async def test_the_search_does_not_leak_a_secret_value(self, app_client, scenario, db):
        """Searching for a credential setting must show the field, not the value."""
        from c2w.settings import settings_service

        async with db() as s:
            await settings_service.set(
                s,
                "alerts.telegram_bot_token",
                "SEARCHLEAKCANARY123456",
                brand_id=None,
                changed_by="test@example.com",
            )
            await s.commit()

        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/admin/settings?q=telegram")
        assert "SEARCHLEAKCANARY123456" not in page.text
        assert "secret" in page.text     # labelled as one

    async def test_a_role_without_a_section_does_not_see_its_settings(
        self, app_client, scenario, db
    ):
        """The rail hides permission-gated sections; search must agree.

        A search that returned what the rail refuses to show would be a way
        round the gate.
        """
        from c2w.auth.rbac import permissions_for
        from c2w.db.models.auth import Role
        from c2w.web.routes import _search_settings

        admin_only = _search_settings("organisation", permissions_for(Role.ADMIN))
        assert all(m["category"] != "Organisations" for m in admin_only)


class TestTheDashboardUpdatesLive:
    """The figures refresh without a page reload.

    A backfill that takes days is the case this is for: the dashboard is the
    thing left open on a second screen, and a number that only moves when
    somebody presses F5 is not a monitor.
    """

    async def test_the_page_asks_for_the_fragment_on_a_timer(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/")
        assert page.status_code == 200
        assert 'hx-get="/dashboard/live"' in page.text
        assert "every 10s" in page.text

    async def test_the_figures_are_present_before_any_polling(
        self, app_client, scenario
    ):
        """Server-rendered first: correct with scripting unavailable."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        page = await app_client.get("/")
        assert "Archive progress" in page.text
        assert "Recordings known" in page.text

    async def test_the_fragment_is_only_the_figures(self, app_client, scenario):
        """No shell, or every poll would swap the navigation into the page."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        fragment = await app_client.get("/dashboard/live")
        assert fragment.status_code == 200
        assert "Archive progress" in fragment.text
        assert "<nav class=\"top\">" not in fragment.text
        assert "<!doctype html>" not in fragment.text.lower()

    async def test_the_fragment_says_when_it_was_produced(self, app_client, scenario):
        """The question a stale-looking dashboard raises."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        fragment = await app_client.get("/dashboard/live")
        assert "Updated" in fragment.text
        assert "live-stamp" in fragment.text

    async def test_the_poll_needs_a_session(self, app_client):
        """It carries figures, so it is not public."""
        refused = await app_client.get("/dashboard/live", follow_redirects=False)
        assert refused.status_code in (401, 302, 303, 307)

    async def test_the_poll_is_scoped_to_the_selected_organisation(
        self, app_client, scenario, db
    ):
        """A fragment that saw across organisations would be a hole.

        The page it comes from is brand-scoped; the poll must be too, or the
        cheap route becomes the way round the boundary.
        """
        import inspect

        from c2w.web.routes import dashboard_live

        # ScopedSession, not a bare session: the dependency is what applies the
        # RLS scope, so asserting on the signature is asserting on the control.
        annotations = inspect.get_annotations(dashboard_live, eval_str=False)
        assert "ScopedSession" in str(annotations["session"])


class TestSignInAlerts:
    """Every sign-in, and every refusal, is announced.

    This console holds two companies' call recordings, so who opened it and
    from where is worth knowing as it happens rather than in a log somebody
    reads afterwards.
    """

    @staticmethod
    def _capture(monkeypatch):
        from c2w.web import routes

        sent = []

        async def _spy(_session, alert):
            sent.append(alert)
            return ["telegram"]

        monkeypatch.setattr(routes, "dispatch", _spy)
        return sent

    async def test_a_successful_sign_in_is_announced(
        self, app_client, scenario, monkeypatch
    ):
        sent = self._capture(monkeypatch)
        await _login(app_client, scenario["admin_email"], scenario["password"])
        titles = [a.title for a in sent]
        assert "Signed in" in titles, titles
        alert = next(a for a in sent if a.title == "Signed in")
        assert alert.fields["Who"] == scenario["admin_email"]
        assert "From" in alert.fields and "Method" in alert.fields

    async def test_a_refused_sign_in_is_announced_as_a_warning(
        self, app_client, scenario, monkeypatch
    ):
        from c2w.alerts.base import Severity

        sent = self._capture(monkeypatch)
        await app_client.post(
            "/login",
            data={"email": scenario["admin_email"], "password": "wrong-password-here"},
            follow_redirects=False,
        )
        refused = [a for a in sent if a.title == "Sign-in refused"]
        assert refused, [a.title for a in sent]
        assert refused[0].severity == Severity.WARNING

    async def test_no_password_ever_reaches_the_alert(
        self, app_client, scenario, monkeypatch
    ):
        """The obvious way to get this wrong."""
        sent = self._capture(monkeypatch)
        await app_client.post(
            "/login",
            data={"email": scenario["admin_email"], "password": "ALERTLEAKCANARY99"},
            follow_redirects=False,
        )
        for alert in sent:
            blob = alert.as_text() + repr(alert.fields)
            assert "ALERTLEAKCANARY99" not in blob

    async def test_a_refusal_does_not_say_whether_the_account_exists(
        self, app_client, monkeypatch
    ):
        """Otherwise the chat answers "is this a real user here?" for anyone.

        Both a wrong password on a real address and an address that does not
        exist must produce the same shape of message.
        """
        sent = self._capture(monkeypatch)
        await app_client.post(
            "/login",
            data={"email": "nobody-here@example.com", "password": "whatever-long-enough"},
            follow_redirects=False,
        )
        refused = [a for a in sent if a.title == "Sign-in refused"]
        assert refused
        text_ = refused[0].as_text().lower()
        for giveaway in ("no such user", "unknown user", "does not exist", "not found"):
            assert giveaway not in text_, text_

    async def test_it_is_a_platform_event_not_an_organisation_one(
        self, app_client, scenario, monkeypatch
    ):
        """A super admin has no organisation, and this is not one company's business."""
        sent = self._capture(monkeypatch)
        await _login(app_client, scenario["admin_email"], scenario["password"])
        alert = next(a for a in sent if a.title == "Signed in")
        assert alert.brand_id is None

    async def test_a_burst_from_one_address_collapses(
        self, app_client, scenario, monkeypatch
    ):
        """Otherwise an attack floods the chat and hides everything else."""
        sent = self._capture(monkeypatch)
        for _ in range(3):
            await app_client.post(
                "/login",
                data={"email": "attacker@example.com", "password": "guess-a-password"},
                follow_redirects=False,
            )
        refused = [a for a in sent if a.title == "Sign-in refused"]
        assert refused
        # One dedupe key for the lot, so `dispatch` suppresses the repeats.
        assert len({a.dedupe_key for a in refused}) == 1

    async def test_alerting_failure_never_blocks_a_sign_in(
        self, app_client, scenario, monkeypatch
    ):
        """Observability failing must not take the thing it observes with it."""
        from c2w.web import routes

        async def _explode(_session, _alert):
            raise RuntimeError("telegram is down")

        monkeypatch.setattr(routes, "dispatch", _explode)
        response = await app_client.post(
            "/login",
            data={"email": scenario["admin_email"], "password": scenario["password"]},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)
        assert response.cookies.get("c2w_session") or "set-cookie" in response.headers

    async def test_both_switches_exist_and_are_platform_wide(self):
        from c2w.settings_spec import SETTINGS

        for key in ("alerts.on_signin", "alerts.on_failed_signin"):
            assert SETTINGS[key].brand_overridable is False, key
            assert SETTINGS[key].default is True, key


class TestTheAccountPanelsAreLaidOutProperly:
    """The archive and CommPeak panels put a whole form in a button tray.

    `.rowactions .menu` is the UI kit's tray for one or two small buttons: a
    right-aligned flex row under a dashed rule. A ten-field edit form was
    being rendered inside it, inside the last table cell, which is why the
    page showed a cramped column pinned to the right with a wide empty gap
    beside it. The component was correct; the use of it was not.
    """

    async def _a_destination(self, db, brand_id):
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            await s.execute(
                text(
                    "INSERT INTO storage_destinations (brand_id, name, provider, "
                    "endpoint, region, bucket, path_prefix, access_key_sealed, "
                    "secret_sealed) VALUES (:b, :n, 'wasabi', "
                    "'https://s3.eu-central-1.wasabisys.com', 'eu-central-1', "
                    ":bk, 'archive', 'x', 'x')"
                ),
                {
                    "b": brand_id,
                    "n": f"arch-{uuid.uuid4().hex[:6]}",
                    "bk": f"b-{uuid.uuid4().hex[:8]}",
                },
            )
            await s.commit()

    async def test_the_edit_form_is_on_its_own_full_width_row(
        self, app_client, scenario, db
    ):
        await self._a_destination(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["admin_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        page = await app_client.get("/admin/storage")
        assert page.status_code == 200
        assert 'class="editrow"' in page.text
        assert "colspan=" in page.text

    def test_no_form_is_left_inside_the_button_tray(self):
        """The kit's tray is for buttons. Guarded on the templates directly.

        A page-level assertion would pass as soon as one panel was fixed; this
        catches the pattern coming back anywhere.
        """
        import re
        from pathlib import Path

        for path in Path("src/c2w/web/templates").glob("*.html"):
            body = path.read_text()
            for block in re.findall(
                r'<div class="menu">(.*?)</div>\s*</details>', body, re.S
            ):
                assert "<label" not in block, f"{path.name} puts a form in .menu"

    def test_a_browser_cannot_autofill_a_stored_credential(self):
        """The bug the screenshot revealed, and the one with consequences.

        Both credential inputs are `type="password"` and empty, with a
        placeholder saying a value is stored. A password manager will fill any
        such field, and the save handler treats a non-empty value as a
        replacement -- so an autofilled entry would silently overwrite a live
        S3 key with whatever the browser had saved for the site. The add forms
        already said `autocomplete="off"`; the edit forms did not.
        """
        import re
        from pathlib import Path

        for name in ("_archive_accounts.html", "_commpeak_accounts.html"):
            body = (Path("src/c2w/web/templates") / name).read_text()
            for field in re.findall(r"<input[^>]*type=\"password\"[^>]*>", body, re.S):
                assert "autocomplete=" in field, f"{name}: unguarded {field[:70]}"

    async def test_the_service_is_named_the_way_its_vendor_spells_it(
        self, app_client, scenario, db
    ):
        """The database stores `wasabi`; the page showed `wasabi`."""
        await self._a_destination(db, scenario["brand_id"])
        await _login(
            app_client,
            scenario["admin_email"],
            scenario["password"],
            brand_id=scenario["brand_id"],
        )
        page = await app_client.get("/admin/storage")
        assert "Wasabi" in page.text

    def test_every_connection_state_has_a_human_label(self):
        """`conn_class` gives the colour; `conn_label` gives the words."""
        from c2w.db.base import ConnectionStatus
        from c2w.web.filters import _CONNECTION_LABEL

        for state in ConnectionStatus:
            assert str(state) in _CONNECTION_LABEL, state
            assert _CONNECTION_LABEL[str(state)] != str(state)


class TestFilteringLeavesAUsableUrl:
    """Filtering a list must not leave the fragment's address in the bar.

    `hx-push-url="true"` pushes the URL htmx requested, and for these tables
    that is the fragment endpoint -- so filtering left
    `/calls/rows?country=Mexico` in the address bar. Reloading it, sharing it,
    or going back and forward rendered a bare `<table>` with no page around
    it: no filter form, no navigation. To an operator that reads as "the
    filters do not work", because the filters were the last thing they
    touched.
    """

    async def test_the_calls_fragment_pushes_the_page_url(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get(
            "/calls/rows?direction=out&limit=10", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200
        pushed = response.headers.get("hx-push-url")
        assert pushed is not None, "nothing corrected the pushed URL"
        assert pushed.startswith("/calls?"), pushed
        assert "/calls/rows" not in pushed
        # The filter has to survive into the pushed address, or a reload drops it.
        assert "direction=out" in pushed

    async def test_the_messages_fragment_pushes_the_page_url(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get(
            "/messages/rows?limit=10", headers={"HX-Request": "true"}
        )
        if response.status_code == 404:
            pytest.skip("messages are not enabled in this scenario")
        pushed = response.headers.get("hx-push-url")
        assert pushed and pushed.startswith("/messages"), pushed
        assert "/messages/rows" not in pushed

    async def test_the_pushed_url_renders_a_whole_page(self, app_client, scenario):
        """The point of the fix: that address must survive a reload."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        fragment = await app_client.get(
            "/calls/rows?direction=out", headers={"HX-Request": "true"}
        )
        page = await app_client.get(fragment.headers["hx-push-url"])
        assert page.status_code == 200
        assert '<nav class="top">' in page.text
        assert 'id="rows"' in page.text

    async def test_a_fragment_with_no_query_still_pushes_the_page(
        self, app_client, scenario
    ):
        """No trailing `?`, which would be an ugly and pointless URL."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/calls/rows", headers={"HX-Request": "true"})
        assert response.headers.get("hx-push-url") == "/calls"


class TestAnEmptyFilterBoxIsNotAnError:
    """The actual reason the filters "did not work".

    A browser submits every field in a form, including the ones nobody
    touched: an untouched `<input type=number>` and a `<select>` sitting on
    its `value=""` option are both sent as empty strings. The routes declared
    those parameters as `int | None`, so FastAPI answered the whole request
    with **422** -- htmx swapped nothing in, and the table went on showing the
    previous unfiltered result while the form displayed the filters the
    operator had just chosen. Nothing looked broken; the filters had simply
    never been asked for. The plain form submission failed the same way, so
    there was no fallback either.
    """

    #: Exactly what the calls form puts on the wire with only two boxes filled.
    BROWSER_QUERY = (
        "media=&number=&date_from=2026-08-01T12:54&date_to=2026-08-31T12:55"
        "&direction=&agent=&country=Benin&queue=&call_type="
        "&min_duration=&connection_id=&limit=50"
    )

    async def test_the_fragment_accepts_what_the_form_sends(
        self, app_client, scenario
    ):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get(
            f"/calls/rows?{self.BROWSER_QUERY}", headers={"HX-Request": "true"}
        )
        assert response.status_code == 200, response.status_code

    async def test_the_page_accepts_what_the_form_sends(self, app_client, scenario):
        """The non-htmx path, which was the only fallback and also 422'd."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get(f"/calls?{self.BROWSER_QUERY}")
        assert response.status_code == 200, response.status_code

    @pytest.mark.parametrize("field", ["min_duration", "connection_id", "limit", "offset"])
    async def test_each_numeric_filter_tolerates_an_empty_value(
        self, app_client, scenario, field
    ):
        """Named individually, because one of them being strict is enough."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get(f"/calls/rows?{field}=")
        assert response.status_code == 200, f"{field} rejected an empty value"

    async def test_rubbish_in_a_numeric_filter_does_not_break_the_page(
        self, app_client, scenario
    ):
        """A hand-edited or stale URL should degrade, not fail."""
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/calls?limit=lots&min_duration=ages")
        assert response.status_code == 200

    async def test_a_filled_numeric_filter_is_still_honoured(self, app_client, scenario):
        """Tolerating empty must not mean ignoring a real value."""
        from c2w.web.routes import _parse_query

        query = _parse_query(min_duration="600", connection_id="7", limit="25")
        assert query.min_duration == 600
        assert query.connection_id == 7
        assert query.limit == 25

    async def test_the_messages_list_tolerates_it_too(self, app_client, scenario):
        await _login(app_client, scenario["admin_email"], scenario["password"])
        response = await app_client.get("/messages/rows?limit=&offset=")
        if response.status_code == 404:
            pytest.skip("messages are not enabled in this scenario")
        assert response.status_code == 200
