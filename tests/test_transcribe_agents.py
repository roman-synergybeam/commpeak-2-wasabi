"""Remote transcription agents: the server's side of lending out the work.

What matters here is what the server refuses to trust the agent with: which
organisation a job belongs to, whether a result is kept, whether a number is
redacted, and whose job it is to hand back. Each of those is asserted against a
real database, because the guarantees are row-level security and leases, and a
mock of either proves nothing.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.transcribe.base import TranscriptResult
from c2w.transcribe.service import TranscribeSettings, skip_reason

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)


def _settings(**overrides) -> TranscribeSettings:
    values = dict(
        enabled=True, engine="faster-whisper-local", model="turbo",
        primary_language="auto-detect", diarize=False, word_timestamps=True,
        redact_numbers=False, only_longer_than=5, concurrency=1, min_speech_seconds=5,
    )
    values.update(overrides)
    return TranscribeSettings(**values)


class TestWhatIsKept:
    """One rule for every recogniser, so a remote agent's transcript is held to
    exactly what the server's own would be."""

    def test_hold_music_is_skipped_as_no_speech(self):
        result = TranscriptResult(text="", duration_seconds=95, speech_seconds=1.5)
        assert skip_reason(result, _settings()).startswith("skipped: no speech")

    def test_a_short_call_is_skipped_for_length(self):
        result = TranscriptResult(text="hola", duration_seconds=3, speech_seconds=None)
        assert "under the 5s minimum" in skip_reason(result, _settings())

    def test_words_the_recogniser_did_not_hear_are_not_a_transcript(self):
        result = TranscriptResult(text="  ", duration_seconds=60, speech_seconds=30)
        assert skip_reason(result, _settings()).startswith("skipped: no speech")

    def test_a_conversation_is_kept(self):
        result = TranscriptResult(text="buenos dias", duration_seconds=60, speech_seconds=30)
        assert skip_reason(result, _settings()) is None

    def test_zero_switches_the_speech_rule_off(self):
        result = TranscriptResult(text="hm", duration_seconds=60, speech_seconds=0.5)
        assert skip_reason(result, _settings(min_speech_seconds=0)) is None


@pytest.fixture
async def platform(monkeypatch):
    """The application's own session factories, pointed at the scratch database."""
    monkeypatch.setenv("C2W_DATABASE_URL", TEST_DB)
    import c2w.config
    import c2w.db.session as session_mod

    c2w.config.get_bootstrap.cache_clear()
    await session_mod.dispose_engine()
    yield
    await session_mod.dispose_engine()


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _brand(db, *, recordings: int = 3, unanswered: int = 0, redact: bool = False) -> dict:
    """An organisation with archived recordings, oldest first by a minute each."""
    from c2w.settings import settings_service

    slug = f"agent-{uuid.uuid4().hex[:8]}"
    async with db() as s:
        brand_id = (
            await s.execute(
                text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
                {"n": slug, "s": slug},
            )
        ).scalar_one()
        for table in ("cdrs", "recordings", "transcripts", "transcript_segments"):
            await s.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand_id} "
                    f"PARTITION OF {table} FOR VALUES IN ({brand_id})"
                )
            )
        await s.commit()
        await settings_service.set(s, "transcribe.enabled", True, brand_id=brand_id)
        await settings_service.set(s, "transcribe.redact_numbers", redact, brand_id=brand_id)
        await s.commit()

        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )
        tenant = (
            await s.execute(
                text("INSERT INTO tenants (brand_id, name, slug) VALUES (:b,'t',:s) RETURNING id"),
                {"b": brand_id, "s": slug},
            )
        ).scalar_one()
        conn = (
            await s.execute(
                text(
                    "INSERT INTO commpeak_connections (brand_id, tenant_id, name, s3_bucket, "
                    "s3_access_key_sealed, s3_secret_sealed) "
                    "VALUES (:b,:t,'c',:s,'x','x') RETURNING id"
                ),
                {"b": brand_id, "t": tenant, "s": slug},
            )
        ).scalar_one()
        dest = (
            await s.execute(
                text(
                    "INSERT INTO storage_destinations (brand_id, name, bucket, endpoint, "
                    "region, access_key_sealed, secret_sealed) "
                    "VALUES (:b, 'archive', :s, 'https://example', 'r', 'x', 'x') RETURNING id"
                ),
                {"b": brand_id, "s": slug},
            )
        ).scalar_one()
        base = datetime(2024, 1, 1, tzinfo=UTC)
        ids = []
        for i in range(recordings + unanswered):
            cdr_id = None
            if i >= recordings:
                cdr_id = (
                    await s.execute(
                        text(
                            "INSERT INTO cdrs (brand_id, connection_id, tenant_id, call_uuid, "
                            "start_at, status, bill_duration) "
                            "VALUES (:b, :c, :t, :u, :st, 'ORIGINATOR_CANCEL', 0) RETURNING id"
                        ),
                        {
                            "b": brand_id, "c": conn, "t": tenant,
                            "u": str(uuid.uuid4()), "st": base + timedelta(minutes=i),
                        },
                    )
                ).scalar_one()
            ids.append(
                (
                    await s.execute(
                        text(
                            "INSERT INTO recordings (brand_id, connection_id, tenant_id, "
                            "source_key, source_size, state, started_at, cdr_id, "
                            "destination_id, destination_key) "
                            "VALUES (:b, :c, :t, :k, 1, 'AVAILABLE', :st, :cdr, :d, :dk) "
                            "RETURNING id"
                        ),
                        {
                            "b": brand_id, "c": conn, "t": tenant, "k": f"k{i}",
                            "st": base + timedelta(minutes=i), "cdr": cdr_id,
                            "d": dest, "dk": f"archive/c/recordings/{i}.flac",
                        },
                    )
                ).scalar_one()
            )
        await s.commit()
    return {"brand_id": brand_id, "recordings": ids}


async def _fake_presign(_session, objects):
    return [f"https://archive.test/{key}?signed" for _dest, key in objects]


async def _agent(name_prefix: str = "pc"):
    from c2w.db.session import get_sessionmaker
    from c2w.transcribe.agents import authenticate, create_agent

    async with get_sessionmaker()() as s:
        token = await create_agent(s, f"{name_prefix}-{uuid.uuid4().hex[:6]}")
        await s.commit()
        agent = await authenticate(s, token)
        await s.commit()
    return token, agent


async def _claim(agent, brand_id: int, limit: int = 10) -> list[dict]:
    from c2w.db.session import brand_session
    from c2w.transcribe.agents import _claim_for_brand

    async with brand_session(brand_id) as s:
        return await _claim_for_brand(s, agent, brand_id, limit, _fake_presign)


async def _transcript(db, brand_id: int, recording_id: int):
    async with db() as s:
        await s.execute(text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)})
        return (
            await s.execute(
                text(
                    "SELECT text, failed_reason, engine FROM transcripts "
                    "WHERE brand_id = :b AND recording_id = :r"
                ),
                {"b": brand_id, "r": recording_id},
            )
        ).first()


@needs_db
class TestTokens:
    async def test_only_the_hash_is_stored(self, platform, db):
        token, agent = await _agent()
        async with db() as s:
            stored = (
                await s.execute(
                    text("SELECT token_hash FROM transcribe_agents WHERE id = :i"), {"i": agent.id}
                )
            ).scalar_one()
        assert token not in stored and len(stored) == 64

    async def test_a_revoked_token_is_refused(self, platform):
        from c2w.db.session import get_sessionmaker
        from c2w.transcribe.agents import authenticate, revoke_agent

        token, agent = await _agent()
        async with get_sessionmaker()() as s:
            assert await revoke_agent(s, agent.name)
            await s.commit()
            assert await authenticate(s, token) is None

    async def test_a_made_up_token_is_refused(self, platform):
        from c2w.db.session import get_sessionmaker
        from c2w.transcribe.agents import authenticate

        async with get_sessionmaker()() as s:
            assert await authenticate(s, "c2wa_" + "x" * 43) is None
            assert await authenticate(s, "not-even-the-shape") is None


@needs_db
class TestClaiming:
    async def test_oldest_first_and_never_twice(self, platform, db):
        brand = await _brand(db, recordings=3)
        _, agent = await _agent()
        first = await _claim(agent, brand["brand_id"], limit=2)
        second = await _claim(agent, brand["brand_id"], limit=10)
        assert [j["audio_url"].split("/")[-1] for j in first] == ["0.flac?signed", "1.flac?signed"]
        assert [j["audio_url"].split("/")[-1] for j in second] == ["2.flac?signed"]

    async def test_the_job_id_names_no_recording(self, platform, db):
        """The agent sees an organisation number and an opaque token -- the
        recording is found again from the lease, never from what it sends."""
        brand = await _brand(db, recordings=1)
        _, agent = await _agent()
        (job,) = await _claim(agent, brand["brand_id"])
        assert str(brand["recordings"][0]) not in job["job_id"].split(".", 1)[1]

    async def test_a_call_nobody_answered_is_skipped_without_a_download(self, platform, db):
        brand = await _brand(db, recordings=1, unanswered=2)
        _, agent = await _agent()
        jobs = await _claim(agent, brand["brand_id"])
        assert len(jobs) == 1
        for rec in brand["recordings"][1:]:
            row = await _transcript(db, brand["brand_id"], rec)
            assert row.failed_reason.startswith("skipped: not answered")

    async def test_an_expired_lease_is_handed_out_again(self, platform, db):
        brand = await _brand(db, recordings=1)
        _, agent = await _agent()
        (job,) = await _claim(agent, brand["brand_id"])
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(brand["brand_id"])},
            )
            await s.execute(
                text("UPDATE transcribe_leases SET expires_at = now() - interval '1 second'")
            )
            await s.commit()
        (again,) = await _claim(agent, brand["brand_id"])
        assert again["job_id"] != job["job_id"]


@needs_db
class TestResults:
    async def test_a_transcript_is_stored_and_redacted_by_the_server(self, platform, db):
        from c2w.transcribe.agents import AgentResult, submit

        brand = await _brand(db, recordings=1, redact=True)
        _, agent = await _agent()
        (job,) = await _claim(agent, brand["brand_id"])
        outcome = await submit(
            agent,
            job["job_id"],
            AgentResult(
                segments=[{"start": 0, "end": 4, "text": "mi tarjeta 4481 2033 9921 0044"}],
                duration_seconds=60, speech_seconds=40, model="turbo",
            ),
        )
        assert outcome == "stored"
        row = await _transcript(db, brand["brand_id"], brand["recordings"][0])
        assert row.text == "mi tarjeta **************44"
        assert row.engine == "faster-whisper-agent"

    async def test_hold_music_from_an_agent_is_recorded_as_no_speech(self, platform, db):
        from c2w.transcribe.agents import AgentResult, submit

        brand = await _brand(db, recordings=1)
        _, agent = await _agent()
        (job,) = await _claim(agent, brand["brand_id"])
        assert await submit(
            agent, job["job_id"], AgentResult(duration_seconds=120, speech_seconds=0.8)
        ) == "skipped"
        row = await _transcript(db, brand["brand_id"], brand["recordings"][0])
        assert row.failed_reason.startswith("skipped: no speech")

    async def test_another_agents_job_is_refused(self, platform, db):
        from c2w.transcribe.agents import AgentResult, LeaseGone, submit

        brand = await _brand(db, recordings=1)
        _, owner = await _agent()
        _, other = await _agent()
        (job,) = await _claim(owner, brand["brand_id"])
        with pytest.raises(LeaseGone):
            await submit(other, job["job_id"], AgentResult(duration_seconds=60))

    async def test_a_job_cannot_be_moved_to_another_organisation(self, platform, db):
        """Rewriting the organisation in the job id finds nothing: the lease is
        under the first organisation's row-level security."""
        from c2w.transcribe.agents import AgentResult, LeaseGone, submit

        a = await _brand(db, recordings=1)
        b = await _brand(db, recordings=1)
        _, agent = await _agent()
        (job,) = await _claim(agent, a["brand_id"])
        forged = f"{b['brand_id']}.{job['job_id'].split('.', 1)[1]}"
        with pytest.raises(LeaseGone):
            await submit(agent, forged, AgentResult(duration_seconds=60))

    async def test_giving_a_job_back_makes_it_available_again(self, platform, db):
        from c2w.transcribe.agents import fail

        brand = await _brand(db, recordings=2)
        _, agent = await _agent()
        first, _second = await _claim(agent, brand["brand_id"])
        await fail(agent, first["job_id"], reason="CUDA out of memory", retry=True)
        (again,) = await _claim(agent, brand["brand_id"])
        assert again["audio_url"].endswith("0.flac?signed")

    async def test_a_permanent_failure_is_recorded_and_not_retried(self, platform, db):
        from c2w.transcribe.agents import fail

        brand = await _brand(db, recordings=1)
        _, agent = await _agent()
        (job,) = await _claim(agent, brand["brand_id"])
        await fail(agent, job["job_id"], reason="unreadable audio", retry=False)
        row = await _transcript(db, brand["brand_id"], brand["recordings"][0])
        assert "unreadable audio" in row.failed_reason
        assert await _claim(agent, brand["brand_id"]) == []


@needs_db
async def test_one_organisation_cannot_read_anothers_leases(platform, db):
    """Leases and cursors are brand-scoped tables; the rule for those is a test
    proving organisation A cannot see organisation B's rows."""
    a = await _brand(db, recordings=1)
    b = await _brand(db, recordings=1)
    _, agent = await _agent()
    await _claim(agent, a["brand_id"])
    async with db() as s:
        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(b["brand_id"])}
        )
        for table in ("transcribe_leases", "transcribe_cursors"):
            rows = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE brand_id = :a"),  # noqa: S608
                    {"a": a["brand_id"]},
                )
            ).scalar_one()
            assert rows == 0, table


@needs_db
class TestTheApi:
    @pytest.fixture
    async def client(self, platform):
        from c2w.api.app import create_app

        transport = ASGITransport(app=create_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    async def test_claiming_needs_a_token(self, client):
        response = await client.post("/api/agent/v1/claim", json={"max": 1})
        assert response.status_code == 401

    async def test_a_valid_token_reaches_progress(self, client):
        token, _ = await _agent()
        response = await client.get(
            "/api/agent/v1/progress", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert isinstance(response.json()["progress"], list)

    async def test_the_installer_names_this_console(self, client):
        response = await client.get("/api/agent/v1/download/install.ps1")
        assert response.status_code == 200
        assert "__C2W_SERVER__" not in response.text

    async def test_only_the_agent_files_are_served(self, client):
        response = await client.get("/api/agent/v1/download/..%2Fapp.py")
        assert response.status_code == 404
