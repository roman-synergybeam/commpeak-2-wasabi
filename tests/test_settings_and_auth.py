"""Database-backed settings and local authentication.

Configuration lives in the database rather than in env files, so these tests
run against a real PostgreSQL: the resolution order, the partial-index upserts
and the append-only history are all database behaviour.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.auth.local import (
    MAX_FAILED_LOGINS,
    AuthError,
    authenticate,
    create_session,
    create_super_admin,
    hash_password,
    resolve_session,
    revoke_all_sessions,
    revoke_session,
    verify_password,
)
from c2w.db.models.auth import AuthSource, Role, User
from c2w.settings import SettingsError, SettingsService
from c2w.settings_spec import SETTINGS

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)


@pytest.fixture
async def session():
    engine = create_async_engine(TEST_DB)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest.fixture
def svc():
    """A fresh service per test, so the cache never carries state between them."""
    return SettingsService()


@pytest.fixture
async def brand(session):
    slug = f"b-{uuid.uuid4().hex[:8]}"
    brand_id = (
        await session.execute(
            text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
            {"n": slug, "s": slug},
        )
    ).scalar_one()
    await session.commit()
    return brand_id


class TestSettingsResolution:
    async def test_unset_setting_returns_registry_default(self, session, svc):
        value = await svc.get(session, "retention.offload_after_days")
        assert value == SETTINGS["retention.offload_after_days"].default == 90

    async def test_global_value_overrides_default(self, session, svc):
        await svc.set(session, "media.presign_ttl_seconds", 600, changed_by="test")
        await session.commit()
        assert await svc.get_int(session, "media.presign_ttl_seconds") == 600

    async def test_brand_override_beats_global(self, session, svc, brand):
        await svc.set(session, "retention.offload_after_days", 90, changed_by="test")
        await svc.set(session, "retention.offload_after_days", 365, brand_id=brand)
        await session.commit()
        assert await svc.get_int(session, "retention.offload_after_days") == 90
        assert await svc.get_int(session, "retention.offload_after_days", brand_id=brand) == 365

    async def test_upsert_replaces_rather_than_duplicating(self, session, svc, brand):
        """The uniqueness rules are partial indexes, so ON CONFLICT has to name
        the predicate; without that this raises instead of updating."""
        for value in (100, 200, 300):
            await svc.set(session, "retention.offload_after_days", value, brand_id=brand)
        await session.commit()
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app_settings "
                    "WHERE key = 'retention.offload_after_days' AND brand_id = :b"
                ),
                {"b": brand},
            )
        ).scalar_one()
        assert rows == 1
        assert await svc.get_int(session, "retention.offload_after_days", brand_id=brand) == 300

    async def test_global_upsert_does_not_collide_with_brand_rows(self, session, svc, brand):
        await svc.set(session, "retention.keep_archive_years", 7)
        await svc.set(session, "retention.keep_archive_years", 10, brand_id=brand)
        await svc.set(session, "retention.keep_archive_years", 8)
        await session.commit()
        assert await svc.get_int(session, "retention.keep_archive_years") == 8
        assert await svc.get_int(session, "retention.keep_archive_years", brand_id=brand) == 10

    async def test_unset_falls_back_to_global(self, session, svc, brand):
        await svc.set(session, "retention.offload_after_days", 90)
        await svc.set(session, "retention.offload_after_days", 400, brand_id=brand)
        await session.commit()
        await svc.unset(session, "retention.offload_after_days", brand_id=brand)
        await session.commit()
        assert await svc.get_int(session, "retention.offload_after_days", brand_id=brand) == 90

    async def test_global_only_setting_rejects_brand_override(self, session, svc, brand):
        """An engine-wide cap protects the host; one brand must not lift it."""
        with pytest.raises(SettingsError, match="global-only"):
            await svc.set(session, "transfer.concurrency_global", 500, brand_id=brand)

    async def test_unknown_key_is_rejected(self, session, svc):
        with pytest.raises(KeyError, match="unknown setting"):
            await svc.get(session, "transfer.does_not_exist")


class TestSettingsValidation:
    async def test_commpeak_concurrency_is_capped(self, session, svc):
        """CommPeak documents 5; a much higher value just earns throttling."""
        with pytest.raises(SettingsError, match="throttled"):
            await svc.set(session, "source.concurrency_per_connection", 50)
        await svc.set(session, "source.concurrency_per_connection", 5)

    async def test_type_coercion_from_form_strings(self, session, svc):
        """Values arriving from an HTML form are strings."""
        await svc.set(session, "transfer.enabled", "on")
        await svc.set(session, "media.presign_ttl_seconds", "450")
        await svc.set(session, "transfer.bandwidth_limit_mbps", "12.5")
        await session.commit()
        assert await svc.get_bool(session, "transfer.enabled") is True
        assert await svc.get_int(session, "media.presign_ttl_seconds") == 450
        assert await svc.get_float(session, "transfer.bandwidth_limit_mbps") == 12.5

    async def test_bad_type_is_rejected(self, session, svc):
        with pytest.raises(SettingsError):
            await svc.set(session, "media.presign_ttl_seconds", "not-a-number")

    async def test_presign_ttl_bounds(self, session, svc):
        with pytest.raises(SettingsError, match="between 30 and 3600"):
            await svc.set(session, "media.presign_ttl_seconds", 86400)


class TestSecretSettings:
    async def test_secret_is_sealed_at_rest(self, session, svc):
        await svc.set(session, "alerts.slack_webhook_url", "https://hooks.example/T/B/XYZ")
        await session.commit()
        stored = (
            await session.execute(
                text("SELECT value::text, is_sealed FROM app_settings WHERE key = :k"),
                {"k": "alerts.slack_webhook_url"},
            )
        ).one()
        assert "hooks.example" not in stored[0], "a secret must never sit in plaintext"
        assert stored[1] is True
        assert (
            await svc.get_secret(session, "alerts.slack_webhook_url")
            == "https://hooks.example/T/B/XYZ"
        )

    async def test_all_effective_never_exposes_secret_values(self, session, svc):
        """The admin page renders this dict; it must not be able to leak a token."""
        await svc.set(session, "alerts.telegram_bot_token", "123456:supersecret")
        await session.commit()
        effective = await svc.all_effective(session)
        assert effective["alerts.telegram_bot_token"] is True
        assert "supersecret" not in str(effective)

    async def test_history_records_the_change_not_the_secret(self, session, svc):
        await svc.set(session, "alerts.telegram_bot_token", "123456:another-secret")
        await session.commit()
        rows = (
            await session.execute(
                text(
                    "SELECT old_value::text, new_value::text FROM setting_history "
                    "WHERE key = 'alerts.telegram_bot_token' ORDER BY id DESC LIMIT 1"
                )
            )
        ).one()
        assert "another-secret" not in " ".join(rows)
        assert rows[1] == '"***"'

    async def test_clearing_a_secret_stores_nothing(self, session, svc):
        await svc.set(session, "alerts.telegram_bot_token", "123456:tmp")
        await svc.set(session, "alerts.telegram_bot_token", "")
        await session.commit()
        assert await svc.get_secret(session, "alerts.telegram_bot_token") == ""

    async def test_credential_settings_ship_empty(self):
        """Tokens are configured by the operator later; nothing is pre-seeded."""
        for key, spec in SETTINGS.items():
            if spec.sensitive:
                assert spec.default == "", f"{key} must default to empty"

    async def test_setting_history_is_append_only(self, session, svc):
        await svc.set(session, "media.proxy_enabled", False)
        await session.commit()
        updated = await session.execute(
            text("UPDATE setting_history SET changed_by = 'tamper'")
        )
        deleted = await session.execute(text("DELETE FROM setting_history"))
        await session.commit()
        assert updated.rowcount == 0
        assert deleted.rowcount == 0


class TestLocalAuth:
    async def test_password_hash_roundtrip(self):
        h = hash_password("correct-horse-battery")
        assert h != "correct-horse-battery"
        assert verify_password(h, "correct-horse-battery")
        assert not verify_password(h, "wrong-password-here")

    async def test_short_passwords_are_refused(self):
        with pytest.raises(AuthError, match="at least 12"):
            hash_password("short")

    async def test_super_admin_is_brand_less(self, session):
        """SUPER_ADMIN spans every brand -- the one role outside the isolation
        boundary, which is why there should be very few of them."""
        email = f"sa-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        assert user.role is Role.SUPER_ADMIN
        assert user.brand_id is None
        assert user.auth_source is AuthSource.LOCAL
        assert user.is_super_admin

    async def test_duplicate_super_admin_is_refused(self, session):
        """Re-running the installer must not silently reset a password."""
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        with pytest.raises(AuthError, match="already exists"):
            await create_super_admin(session, email, "different-password-here")

    async def test_authenticate_accepts_correct_password(self, session):
        email = f"ok-{uuid.uuid4().hex[:8]}@example.com"
        await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        user = await authenticate(session, email, "a-long-enough-password")
        assert user.email == email
        assert user.last_login_at is not None

    async def test_authenticate_rejects_wrong_password(self, session):
        email = f"bad-{uuid.uuid4().hex[:8]}@example.com"
        await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        with pytest.raises(AuthError, match="invalid email or password"):
            await authenticate(session, email, "not-the-password")

    async def test_unknown_user_gives_the_same_error(self, session):
        """Identical wording, so the login form cannot enumerate accounts."""
        with pytest.raises(AuthError, match="invalid email or password"):
            await authenticate(session, "nobody@example.com", "whatever-password")

    async def test_repeated_failures_lock_the_account(self, session):
        """These credentials guard recorded phone calls; unlimited online
        guessing is not acceptable."""
        email = f"lock-{uuid.uuid4().hex[:8]}@example.com"
        await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        for _ in range(MAX_FAILED_LOGINS):
            with pytest.raises(AuthError):
                await authenticate(session, email, "wrong-password-value")
        with pytest.raises(AuthError, match="locked"):
            await authenticate(session, email, "a-long-enough-password")

    async def test_disabled_user_cannot_authenticate(self, session):
        email = f"off-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        user.is_active = False
        await session.commit()
        with pytest.raises(AuthError, match="disabled"):
            await authenticate(session, email, "a-long-enough-password")


class TestSessions:
    async def test_session_roundtrip(self, session):
        email = f"s-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        token, row = await create_session(session, user, ip="10.0.0.1", user_agent="pytest")
        await session.commit()
        assert row.token_hash != token, "only the hash may be stored"
        resolved = await resolve_session(session, token)
        assert resolved is not None and resolved.id == user.id

    async def test_revoked_session_stops_working_immediately(self, session):
        email = f"r-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        token, _ = await create_session(session, user)
        await session.commit()
        await revoke_session(session, token)
        await session.commit()
        assert await resolve_session(session, token) is None

    async def test_deactivating_a_user_invalidates_live_sessions(self, session):
        """Revocation must take effect on the next request, not at token expiry."""
        email = f"d-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        token, _ = await create_session(session, user)
        await session.commit()
        user.is_active = False
        await session.commit()
        assert await resolve_session(session, token) is None

    async def test_revoke_all_sessions(self, session):
        email = f"all-{uuid.uuid4().hex[:8]}@example.com"
        user = await create_super_admin(session, email, "a-long-enough-password")
        await session.commit()
        tokens = []
        for _ in range(3):
            token, _ = await create_session(session, user)
            tokens.append(token)
        await session.commit()
        await revoke_all_sessions(session, user.id)
        await session.commit()
        for token in tokens:
            assert await resolve_session(session, token) is None

    async def test_garbage_token_is_rejected(self, session):
        assert await resolve_session(session, "not-a-real-token") is None
        assert await resolve_session(session, "") is None


class TestUserConstraints:
    async def test_non_super_admin_needs_a_brand(self, session, brand):
        """Anyone but SUPER_ADMIN without a brand would sit outside isolation."""
        from sqlalchemy.exc import IntegrityError

        session.add(
            User(
                brand_id=None,
                email=f"nb-{uuid.uuid4().hex[:8]}@example.com",
                role=Role.SUPERVISOR,
                auth_source=AuthSource.LOCAL,
                password_hash=hash_password("a-long-enough-password"),
            )
        )
        with pytest.raises(IntegrityError, match="ck_users_brand_required"):
            await session.commit()
        await session.rollback()

    async def test_local_user_must_have_a_password(self, session, brand):
        from sqlalchemy.exc import IntegrityError

        session.add(
            User(
                brand_id=brand,
                email=f"np-{uuid.uuid4().hex[:8]}@example.com",
                role=Role.AGENT,
                auth_source=AuthSource.LOCAL,
                password_hash=None,
            )
        )
        with pytest.raises(IntegrityError, match="ck_users_credentials_match_source"):
            await session.commit()
        await session.rollback()

    async def test_federated_user_must_not_have_a_local_password(self, session, brand):
        """Once AD is in place, a federated identity carrying a local password
        would be a second, unmanaged way in."""
        from sqlalchemy.exc import IntegrityError

        session.add(
            User(
                brand_id=brand,
                email=f"fed-{uuid.uuid4().hex[:8]}@example.com",
                role=Role.AGENT,
                auth_source=AuthSource.ENTRA,
                oidc_issuer="https://login.microsoftonline.com/x/v2.0",
                oidc_subject=uuid.uuid4().hex,
                password_hash=hash_password("a-long-enough-password"),
            )
        )
        with pytest.raises(IntegrityError, match="ck_users_credentials_match_source"):
            await session.commit()
        await session.rollback()
