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
