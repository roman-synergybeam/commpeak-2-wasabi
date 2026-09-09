"""The second factor, and the people page that can now create accounts.

Unit coverage for the algorithm sits beside end-to-end coverage of the flow,
because the interesting failures live in different places. The arithmetic is
checked against RFC 6238's own published vectors -- an implementation that
produces plausible six-digit numbers but not *these* six-digit numbers works
perfectly against itself and against nothing else.

The flow tests exist because the security properties are all about ordering:
that a password alone yields no session, that a code cannot be replayed, that
an unconfirmed secret is never demanded. None of those are visible from
reading a single function.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.auth import totp
from c2w.auth.local import create_super_admin, hash_password
from c2w.db.models.auth import AuthSource, Role, User

# --------------------------------------------------------------------- unit


class TestRfcVectors:
    """RFC 6238 Appendix B, the SHA-1 column.

    The seed is the ASCII string "12345678901234567890", which the RFC gives
    as hex; base32 is what an authenticator actually consumes.
    """

    SEED = base64.b32encode(b"12345678901234567890").decode().rstrip("=")

    @pytest.mark.parametrize(
        "unix_time,expected",
        [
            (59, "287082"),
            (1111111109, "081804"),
            (1111111111, "050471"),
            (1234567890, "005924"),
            (2000000000, "279037"),
            (20000000000, "353130"),
        ],
    )
    def test_matches_the_rfc(self, unix_time: int, expected: str) -> None:
        assert totp.code_at(self.SEED, unix_time // totp.STEP_SECONDS) == expected


class TestVerification:
    def test_accepts_the_current_code(self) -> None:
        secret = totp.new_secret()
        now = 1_700_000_000
        code = totp.code_at(secret, now // 30)
        assert totp.verify(secret, code, at=now) == now // 30

    def test_tolerates_a_step_of_clock_drift(self) -> None:
        """A phone half a minute out still works; three steps out does not."""
        secret = totp.new_secret()
        now = 1_700_000_000
        for drift in (-1, 0, 1):
            code = totp.code_at(secret, now // 30 + drift)
            assert totp.verify(secret, code, at=now) is not None, drift
        for drift in (-3, 3):
            code = totp.code_at(secret, now // 30 + drift)
            assert totp.verify(secret, code, at=now) is None, drift

    def test_a_code_cannot_be_used_twice(self) -> None:
        """The property that makes shoulder-surfing a 30-second code useless."""
        secret = totp.new_secret()
        now = 1_700_000_000
        code = totp.code_at(secret, now // 30)
        counter = totp.verify(secret, code, at=now)
        assert counter is not None
        assert totp.verify(secret, code, last_counter=counter, at=now) is None

    def test_an_older_code_is_refused_once_a_newer_one_was_used(self) -> None:
        secret = totp.new_secret()
        now = 1_700_000_000
        used = totp.verify(secret, totp.code_at(secret, now // 30), at=now)
        earlier = totp.code_at(secret, now // 30 - 1)
        assert totp.verify(secret, earlier, last_counter=used, at=now) is None

    def test_rejects_junk(self) -> None:
        secret = totp.new_secret()
        for entered in ("", "12345", "abcdef", "1234567", None):
            assert totp.verify(secret, entered or "", at=1_700_000_000) is None

    def test_spaces_are_forgiven(self) -> None:
        """People paste "123 456" out of an authenticator."""
        secret = totp.new_secret()
        now = 1_700_000_000
        code = totp.code_at(secret, now // 30)
        assert totp.verify(secret, f"{code[:3]} {code[3:]}", at=now) is not None


class TestSecretsAndCodes:
    def test_secret_is_base32_and_long_enough(self) -> None:
        secret = totp.new_secret()
        assert len(secret) == 32          # 160 bits, unpadded
        assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")

    def test_secrets_differ(self) -> None:
        assert len({totp.new_secret() for _ in range(50)}) == 50

    def test_typed_form_round_trips(self) -> None:
        """What is printed for hand-entry must still verify."""
        secret = totp.new_secret()
        typed = totp.format_secret(secret)
        assert " " in typed
        now = 1_700_000_000
        assert totp.verify(typed, totp.code_at(secret, now // 30), at=now) is not None

    def test_provisioning_uri_carries_what_apps_read(self) -> None:
        uri = totp.provisioning_uri("ABCDEFGH", account="a@b.example", issuer="c2w")
        assert uri.startswith("otpauth://totp/")
        assert "secret=ABCDEFGH" in uri
        assert "issuer=c2w" in uri
        # The issuer belongs in the label too: some apps read only one of them.
        assert "c2w%3Aa%40b.example" in uri

    def test_qr_is_inline_svg_that_follows_the_theme(self) -> None:
        svg = totp.qr_svg(totp.provisioning_uri(totp.new_secret(), account="a@b", issuer="c"))
        assert svg.startswith("<svg")          # no XML prolog: this goes inside HTML
        assert "currentColor" in svg           # inverts in the dark theme
        assert "#010203" not in svg            # the sentinel was replaced

    def test_recovery_codes_are_unique_and_single_use(self) -> None:
        codes = totp.new_recovery_codes()
        assert len(codes) == len(set(codes)) == 10
        hashes = [totp.hash_recovery_code(c) for c in codes]
        matched = totp.verify_recovery_code(hashes, codes[4])
        assert matched == hashes[4]
        hashes.remove(matched)
        assert totp.verify_recovery_code(hashes, codes[4]) is None

    def test_recovery_codes_ignore_case_and_dashes(self) -> None:
        codes = totp.new_recovery_codes(3)
        hashes = [totp.hash_recovery_code(c) for c in codes]
        typed = codes[1].upper().replace("-", "")
        assert totp.verify_recovery_code(hashes, typed) == hashes[1]

    def test_a_recovery_code_is_not_stored_in_the_clear(self) -> None:
        code = totp.new_recovery_codes(1)[0]
        assert code not in totp.hash_recovery_code(code)


# ------------------------------------------------------------------- end to end

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)


@pytest.fixture
async def app_client(monkeypatch):
    monkeypatch.setenv("C2W_DATABASE_URL", TEST_DB)
    monkeypatch.setenv("C2W_MASTER_KEY", base64.b64encode(os.urandom(32)).decode())
    import c2w.config
    import c2w.db.session as session_mod

    c2w.config.get_bootstrap.cache_clear()
    await session_mod.dispose_engine()

    from c2w.api.app import create_app

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await session_mod.dispose_engine()
    c2w.config.get_bootstrap.cache_clear()


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def people(db):
    """A platform admin, an organisation admin and an operator in one brand."""
    slug = f"mfa-{uuid.uuid4().hex[:8]}"
    password = "a-long-enough-password"
    async with db() as s:
        brand_id = (
            await s.execute(
                text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
                {"n": slug, "s": slug},
            )
        ).scalar_one()
        await s.commit()

        platform = await create_super_admin(s, f"plat-{slug}@example.com", password)
        org_admin = User(
            brand_id=brand_id, email=f"orgadmin-{slug}@example.com", display_name="Org admin",
            role=Role.ADMIN, auth_source=AuthSource.LOCAL,
            password_hash=hash_password(password),
        )
        operator = User(
            brand_id=brand_id, email=f"op-{slug}@example.com", display_name="Operator",
            role=Role.OPERATOR, auth_source=AuthSource.LOCAL,
            password_hash=hash_password(password),
        )
        s.add_all([org_admin, operator])
        await s.commit()
    return {
        "brand_id": brand_id,
        "password": password,
        "platform": platform.email,
        "platform_id": platform.id,
        "org_admin": org_admin.email,
        "operator": operator.email,
        "operator_id": operator.id,
    }


async def _current_code(db, email: str, *, ahead: int = 0) -> str:
    """The code an authenticator holding this account's secret would show."""
    from c2w.crypto import open_global

    async with db() as s:
        user = (await s.execute(select(User).where(User.email == email))).scalar_one()
        secret = open_global(user.totp_secret_sealed, aad=f"totp:{user.id}")
    return totp.code_at(secret, int(time.time() // totp.STEP_SECONDS) + ahead)


@needs_db
class TestEnrolmentFlow:
    async def test_a_password_alone_still_signs_in_when_no_factor_is_set(
        self, app_client, people
    ):
        response = await app_client.post(
            "/login",
            data={"email": people["operator"], "password": people["password"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "c2w_session" in response.cookies

    async def test_enrolment_needs_a_working_code_before_it_takes_effect(
        self, app_client, people, db
    ):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        started = await app_client.post("/me/2fa/start", follow_redirects=False)
        assert started.status_code == 303

        page = await app_client.get("/me/2fa")
        assert "<svg" in page.text          # a QR to scan
        assert "currentColor" in page.text

        # A pending secret exists, but the factor is not active and must not be
        # demanded: an abandoned setup cannot lock anyone out.
        async with db() as s:
            user = (
                await s.execute(select(User).where(User.email == people["operator"]))
            ).scalar_one()
            assert user.totp_secret_sealed
            assert user.totp_enrolled_at is None
            assert user.totp_active is False

        wrong = await app_client.post("/me/2fa/confirm", data={"code": "000000"},
                                      follow_redirects=False)
        assert "error=" in wrong.headers["location"]

        code = await _current_code(db, people["operator"])
        confirmed = await app_client.post("/me/2fa/confirm", data={"code": code})
        assert confirmed.status_code == 200
        # Ten recovery codes, shown exactly once.
        assert confirmed.text.count("<li>") == 10

        async with db() as s:
            user = (
                await s.execute(select(User).where(User.email == people["operator"]))
            ).scalar_one()
            assert user.totp_active is True
            assert len(user.totp_recovery_hashes) == 10

    async def test_the_secret_is_sealed_not_stored_in_the_clear(
        self, app_client, people, db
    ):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        await app_client.post("/me/2fa/start")
        page = await app_client.get("/me/2fa")
        shown = page.text.split('class="secret">')[1].split("<")[0].replace(" ", "")

        async with db() as s:
            user = (
                await s.execute(select(User).where(User.email == people["operator"]))
            ).scalar_one()
        assert shown not in (user.totp_secret_sealed or "")


@needs_db
class TestSignInWithASecondFactor:
    @pytest.fixture
    async def enrolled(self, app_client, people, db):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        await app_client.post("/me/2fa/start")
        page = await app_client.post(
            "/me/2fa/confirm", data={"code": await _current_code(db, people["operator"])}
        )
        codes = [
            line.split("<li>")[1].split("</li>")[0]
            for line in page.text.splitlines()
            if "<li>" in line
        ]
        await app_client.get("/logout")
        app_client.cookies.clear()
        return codes

    async def test_password_alone_yields_no_session(self, app_client, people, enrolled):
        response = await app_client.post(
            "/login",
            data={"email": people["operator"], "password": people["password"]},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login/code"
        # The half-login is a ticket, never a session.
        assert "c2w_session" not in response.cookies
        assert "c2w_mfa" in response.cookies

        blocked = await app_client.get(
            "/calls", headers={"accept": "application/json"}, follow_redirects=False
        )
        assert blocked.status_code == 401

    async def test_a_correct_code_completes_the_sign_in(
        self, app_client, people, enrolled, db
    ):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        # One step ahead: enrolment consumed the current one, and a used code
        # must stay used. Still inside the drift window, so it is accepted.
        code = await _current_code(db, people["operator"], ahead=1)
        done = await app_client.post("/login/code", data={"code": code},
                                     follow_redirects=False)
        assert done.status_code == 303
        assert done.headers["location"] == "/"
        assert "c2w_session" in done.cookies

    async def test_a_code_cannot_be_replayed_on_a_second_sign_in(
        self, app_client, people, enrolled, db
    ):
        code = await _current_code(db, people["operator"], ahead=1)
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        first = await app_client.post("/login/code", data={"code": code},
                                      follow_redirects=False)
        assert first.status_code == 303

        app_client.cookies.clear()
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        again = await app_client.post("/login/code", data={"code": code},
                                      follow_redirects=False)
        assert again.status_code == 401

    async def test_a_wrong_code_does_not_sign_anyone_in(self, app_client, people, enrolled):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        response = await app_client.post("/login/code", data={"code": "000000"},
                                         follow_redirects=False)
        assert response.status_code == 401
        assert "c2w_session" not in response.cookies

    async def test_a_recovery_code_works_once(self, app_client, people, enrolled):
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        used = enrolled[0]
        first = await app_client.post("/login/code", data={"code": used},
                                      follow_redirects=False)
        assert first.status_code == 303

        app_client.cookies.clear()
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        second = await app_client.post("/login/code", data={"code": used},
                                       follow_redirects=False)
        assert second.status_code == 401

    async def test_no_ticket_means_back_to_the_start(self, app_client, people, enrolled):
        """Posting a code without having passed the password step goes nowhere."""
        app_client.cookies.clear()
        response = await app_client.post("/login/code", data={"code": "123456"},
                                         follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    async def test_a_forged_ticket_is_refused(self, app_client, people, enrolled):
        app_client.cookies.clear()
        app_client.cookies.set("c2w_mfa", f"{people['operator_id']}.forged.signature")
        response = await app_client.post("/login/code", data={"code": "123456"},
                                         follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


@pytest.fixture
async def require_totp(db):
    """Turn the requirement on, and off again on its own session.

    Its own session on purpose: an earlier version reset the setting inside the
    same session the test had used, and when a test failed with a database error
    that session was already poisoned, so the reset silently never committed.
    The requirement then leaked into every later test in the file, which failed
    for a reason that had nothing to do with what they were checking.
    """
    from c2w.settings import settings_service

    async with db() as s:
        await settings_service.set(s, "mfa.require_totp", True, changed_by="test")
        await s.commit()
    settings_service.invalidate()
    try:
        yield
    finally:
        async with db() as s:
            await settings_service.set(s, "mfa.require_totp", False, changed_by="test")
            await s.commit()
        settings_service.invalidate()


@needs_db
class TestRequiredEnrolment:
    async def test_the_requirement_blocks_until_an_authenticator_works(
        self, app_client, people, db, require_totp
    ):
        response = await app_client.post(
            "/login",
            data={"email": people["operator"], "password": people["password"]},
            follow_redirects=False,
        )
        assert response.headers["location"] == "/login/enrol"
        assert "c2w_session" not in response.cookies

        blocked = await app_client.get(
            "/calls", headers={"accept": "application/json"}, follow_redirects=False
        )
        assert blocked.status_code == 401

        page = await app_client.get("/login/enrol")
        assert "<svg" in page.text

        code = await _current_code(db, people["operator"])
        done = await app_client.post("/login/enrol", data={"code": code})
        assert done.status_code == 200
        assert done.text.count("<li>") == 10        # recovery codes
        assert "c2w_session" in done.cookies

    async def test_a_federated_account_is_not_forced_to_enrol(self, db, require_totp, people):
        """Their directory already carries whatever factor it enforces.

        Built as a detached object rather than by editing the stored row: the
        schema has a check constraint (``ck_users_credentials_match_source``)
        that forbids a federated account holding a password hash, and rightly
        so. Nothing here needs to be persisted to answer the question.
        """
        from c2w.auth import mfa

        federated = User(
            id=-1,
            brand_id=people["brand_id"],
            email="entra@example.com",
            role=Role.OPERATOR,
            auth_source=AuthSource.ENTRA,
            password_hash=None,
        )
        async with db() as s:
            assert await mfa.second_factor_required(s, federated) is False

    async def test_someone_who_opted_in_is_asked_even_when_it_is_not_required(
        self, db, people
    ):
        """A global switch being off must not stop asking someone who chose it."""
        from c2w.auth import mfa

        opted_in = User(
            id=-2,
            brand_id=people["brand_id"],
            email="optedin@example.com",
            role=Role.OPERATOR,
            auth_source=AuthSource.LOCAL,
            totp_secret_sealed="sealed",
            totp_enrolled_at=datetime.now(UTC),
        )
        async with db() as s:
            assert await mfa.second_factor_required(s, opted_in) is True


@needs_db
class TestAdministratorReset:
    async def test_only_a_platform_admin_can_clear_someone_elses_factor(
        self, app_client, people, db
    ):
        # Enrol the operator first, so there is something to clear.
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        await app_client.post("/me/2fa/start")
        await app_client.post(
            "/me/2fa/confirm", data={"code": await _current_code(db, people["operator"])}
        )
        await app_client.get("/logout")
        app_client.cookies.clear()

        await app_client.post(
            "/login", data={"email": people["org_admin"], "password": people["password"]}
        )
        refused = await app_client.post(
            f"/admin/users/{people['operator_id']}/2fa/reset", follow_redirects=False
        )
        assert "error=" in refused.headers["location"]
        async with db() as s:
            user = (
                await s.execute(select(User).where(User.id == people["operator_id"]))
            ).scalar_one()
            assert user.totp_active is True

        app_client.cookies.clear()
        await app_client.post(
            "/login", data={"email": people["platform"], "password": people["password"]}
        )
        allowed = await app_client.post(
            f"/admin/users/{people['operator_id']}/2fa/reset", follow_redirects=False
        )
        assert "saved=" in allowed.headers["location"]
        async with db() as s:
            user = (
                await s.execute(select(User).where(User.id == people["operator_id"]))
            ).scalar_one()
            assert user.totp_active is False
            assert user.totp_recovery_hashes == []

    async def test_nobody_clears_their_own(self, app_client, people):
        await app_client.post(
            "/login", data={"email": people["platform"], "password": people["password"]}
        )
        response = await app_client.post(
            f"/admin/users/{people['platform_id']}/2fa/reset", follow_redirects=False
        )
        assert "error=" in response.headers["location"]


@needs_db
class TestAdministrationIsAudited:
    """Account administration belongs in the append-only trail, not a log file.

    Media access was audited from the start; administration was not, so
    creating an account, disabling one or clearing somebody's second factor
    left no durable record. A log file is rotated and is writable by whoever
    reaches the disk; `audit_events` has UPDATE and DELETE revoked precisely so
    it can answer "who gave this person access".
    """

    async def _actions(self, db, brand_id: int) -> list[tuple[str, str, dict]]:
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            rows = (
                await s.execute(
                    text(
                        "SELECT action, result, detail FROM audit_events "
                        "ORDER BY id DESC LIMIT 20"
                    )
                )
            ).all()
        return [(r[0], r[1], r[2]) for r in rows]

    async def test_creating_and_disabling_an_account_is_recorded(
        self, app_client, people, db
    ):
        await app_client.post(
            "/login", data={"email": people["platform"], "password": people["password"]}
        )
        app_client.cookies.set("c2w_brand", str(people["brand_id"]))

        email = f"audited-{uuid.uuid4().hex[:6]}@example.com"
        created = await app_client.post(
            "/admin/users",
            data={
                "email": email, "auth_source": "LOCAL", "role": "OPERATOR",
                "brand_id": str(people["brand_id"]),
                "password": "a-long-enough-password",
                "password_again": "a-long-enough-password",
            },
            follow_redirects=False,
        )
        assert "saved=" in created.headers["location"], created.headers["location"]

        actions = await self._actions(db, people["brand_id"])
        creation = [a for a in actions if a[0] == "USER_CREATED"]
        assert creation, [a[0] for a in actions]
        # SUCCESS, not a third vocabulary: the audit page treats anything else
        # as a refusal.
        assert creation[0][1] == "SUCCESS"
        assert creation[0][2]["target_email"] == email

        async with db() as s:
            new_id = (
                await s.execute(select(User.id).where(User.email == email))
            ).scalar_one()
        await app_client.post(
            f"/admin/users/{new_id}/active", data={"active": "0"}, follow_redirects=False
        )
        actions = await self._actions(db, people["brand_id"])
        assert any(a[0] == "USER_DISABLED" for a in actions), [a[0] for a in actions]

    async def test_clearing_someone_elses_second_factor_is_recorded(
        self, app_client, people, db
    ):
        """The most abusable action in the system, so the one most worth keeping."""
        await app_client.post(
            "/login", data={"email": people["operator"], "password": people["password"]}
        )
        await app_client.post("/me/2fa/start")
        await app_client.post(
            "/me/2fa/confirm", data={"code": await _current_code(db, people["operator"])}
        )
        app_client.cookies.clear()

        await app_client.post(
            "/login", data={"email": people["platform"], "password": people["password"]}
        )
        app_client.cookies.set("c2w_brand", str(people["brand_id"]))
        await app_client.post(
            f"/admin/users/{people['operator_id']}/2fa/reset", follow_redirects=False
        )

        actions = await self._actions(db, people["brand_id"])
        reset = [a for a in actions if a[0] == "MFA_RESET_BY_ADMIN"]
        assert reset, [a[0] for a in actions]
        assert reset[0][2]["target_user_id"] == people["operator_id"]
        # Turning it on is recorded too, by the person who did it.
        assert any(a[0] == "MFA_ENABLED" for a in actions)

    async def test_the_trail_cannot_be_edited(self, app_client, db, people):
        """An audit trail you can change is not one.

        Enforced with PostgreSQL rules (`ON UPDATE/DELETE DO INSTEAD NOTHING`),
        so an attempt is *discarded* rather than refused: the statement reports
        zero rows affected and raises nothing. That fails in the safe
        direction -- the row survives -- but it does mean a stray
        `DELETE FROM audit_events` looks like it worked on an empty table. What
        matters, and what is asserted here, is that the row is still there
        afterwards and still says what it said.
        """
        await app_client.post(
            "/login", data={"email": people["platform"], "password": people["password"]}
        )
        app_client.cookies.set("c2w_brand", str(people["brand_id"]))
        email = f"tamper-{uuid.uuid4().hex[:6]}@example.com"
        await app_client.post(
            "/admin/users",
            data={
                "email": email, "auth_source": "LOCAL", "role": "OPERATOR",
                "brand_id": str(people["brand_id"]),
                "password": "a-long-enough-password",
                "password_again": "a-long-enough-password",
            },
        )

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(people["brand_id"])},
            )
            before = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM audit_events "
                        "WHERE detail->>'target_email' = :e"
                    ),
                    {"e": email},
                )
            ).scalar_one()
            assert before == 1, "the account creation was not recorded"

            await s.execute(text("UPDATE audit_events SET action = 'TAMPERED'"))
            await s.execute(text("DELETE FROM audit_events"))
            await s.commit()

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(people["brand_id"])},
            )
            row = (
                await s.execute(
                    text(
                        "SELECT action, result FROM audit_events "
                        "WHERE detail->>'target_email' = :e"
                    ),
                    {"e": email},
                )
            ).all()
        assert len(row) == 1, "an audit row was deleted"
        assert row[0][0] == "USER_CREATED", "an audit row was altered"
        assert row[0][1] == "SUCCESS"
