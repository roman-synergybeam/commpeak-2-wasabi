"""Turning call audio into searchable text.

The recogniser itself is somebody else's model, so what is worth testing here
is everything around it: that the settings menu's labels map to codes Whisper
understands, that redaction masks account numbers without mangling dates, that
an unimplemented engine says so instead of silently doing nothing, and that a
stored transcript lands in the shape the search indexes expect.

The engine is driven for real -- faster-whisper with the `tiny` model over a
generated file -- in one test, marked slow, because "the adapter returns the
shape we store" is only worth asserting against the actual library.
"""

from __future__ import annotations

import math
import struct
import uuid
import wave
from pathlib import Path

import pytest
from sqlalchemy import text

from c2w.transcribe.base import EngineUnavailable, Segment, TranscriptResult, redact_digits
from c2w.transcribe.service import IMPLEMENTED_ENGINES, TranscribeSettings, build_engine
from c2w.transcribe.whisper_local import WhisperLocal, _confidence, _language_code


class TestRedaction:
    """A card number spoken aloud is the most sensitive thing in a sales call,
    and a transcript is far easier to copy out than audio."""

    @pytest.mark.parametrize(
        "spoken,expected",
        [
            ("my card is 4481 2033 9921 0044 ok", "my card is **************44 ok"),
            ("call me on 447700900123", "call me on **********23"),
            ("reference 12-34-56-78", "reference ******78"),
            ("account 1234567 please", "account *****67 please"),
        ],
    )
    def test_long_runs_are_masked(self, spoken: str, expected: str):
        assert redact_digits(spoken) == expected

    @pytest.mark.parametrize(
        "spoken",
        [
            "the date is 2026 and it cost 450",
            "six digits 123456 stay",
            "extension 101 answered",
            "nothing numeric here",
        ],
    )
    def test_short_runs_are_left_alone(self, spoken: str):
        """Masking dates and amounts would make a transcript unreadable for no
        gain -- seven digits is the threshold where a run stops being one."""
        assert redact_digits(spoken) == spoken

    def test_the_tail_is_kept(self):
        """"ending 44" is what makes a redacted transcript still findable."""
        assert redact_digits("4481203399210044").endswith("44")

    def test_the_surrounding_text_survives(self):
        """An earlier form swallowed the space after the number."""
        out = redact_digits("card 4481 2033 9921 0044 expires soon")
        assert out.endswith(" expires soon")


class TestLanguageMapping:
    @pytest.mark.parametrize(
        "label,code",
        [
            ("auto-detect", None),
            ("", None),
            ("en — English", "en"),
            ("es — Spanish (Latin America)", "es"),
            ("es-ES — Spanish (Spain)", "es"),
            ("pt-BR — Portuguese (Brazil)", "pt"),
            ("pt-PT — Portuguese (Portugal)", "pt"),
        ],
    )
    def test_menu_labels_become_whisper_codes(self, label: str, code: str | None):
        """The menu reads "pt-BR — Portuguese (Brazil)" because that is what a
        person chooses between; Whisper wants "pt" and silently ignores a
        regional variant."""
        assert _language_code(label) == code


class TestEngineSelection:
    def _settings(self, engine: str) -> TranscribeSettings:
        return TranscribeSettings(
            enabled=True, engine=engine, model="tiny", primary_language="auto-detect",
            diarize=False, word_timestamps=True, redact_numbers=False,
            only_longer_than=0, concurrency=1,
        )

    @pytest.mark.parametrize("engine", sorted(IMPLEMENTED_ENGINES))
    def test_the_local_engines_build(self, engine: str):
        assert isinstance(build_engine(self._settings(engine)), WhisperLocal)

    @pytest.mark.parametrize(
        "engine", ["azure-speech", "google-speech", "aws-transcribe", "openai-whisper-api"]
    )
    def test_an_unbuilt_engine_says_so(self, engine: str):
        """The settings menu offers six; two are built. Selecting one of the
        others must not look like it worked."""
        with pytest.raises(EngineUnavailable, match="not built"):
            build_engine(self._settings(engine))

    def test_every_menu_option_is_either_built_or_refused(self):
        """No option may fall through to something unexpected."""
        from c2w.settings_spec import SETTINGS

        for option in SETTINGS["transcribe.engine"].choices:
            if option in IMPLEMENTED_ENGINES:
                build_engine(self._settings(option))
            else:
                with pytest.raises(EngineUnavailable):
                    build_engine(self._settings(option))


class TestConfidence:
    def test_a_log_probability_becomes_a_bounded_number(self):
        """Whisper reports a log probability, which nobody can read."""
        assert _confidence(None) is None
        assert _confidence(0.0) == 1.0
        assert 0.0 < _confidence(-1.0) < 1.0
        assert _confidence(-100.0) == 0.0


@pytest.mark.slow
class TestTheRealEngine:
    """Driven against faster-whisper itself, with the smallest model.

    Worth the seconds it costs: "the adapter returns the shape we store" is
    only meaningful against the actual library, and a signature change there
    would otherwise surface in production.
    """

    def _wav(self, tmp_path: Path, seconds: float = 1.0) -> str:
        path = tmp_path / "sample.wav"
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            frames = b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * t / 16000)))
                for t in range(int(16000 * seconds))
            )
            w.writeframes(frames)
        return str(path)

    async def test_it_returns_a_storable_result(self, tmp_path):
        engine = WhisperLocal(model="tiny")
        result = await engine.transcribe(self._wav(tmp_path))
        assert isinstance(result, TranscriptResult)
        assert result.engine == "faster-whisper-local"
        assert result.model == "tiny"
        # A tone has no speech, so no segments -- and that must not be an error.
        assert isinstance(result.segments, list)
        assert result.duration_seconds is not None
        assert "language_probability" in result.engine_detail

    async def test_no_speech_stops_before_the_model(self, tmp_path):
        """Hold music and silence end at the speech detector. A model name that
        cannot load proves the recogniser was never reached."""
        engine = WhisperLocal(model="no-such-model")
        result = await engine.transcribe(self._wav(tmp_path, 3.0), min_speech_seconds=5)
        assert result.segments == []
        assert result.speech_seconds is not None and result.speech_seconds < 5
        assert result.duration_seconds == pytest.approx(3.0, abs=0.1)


TEST_DB = __import__("os").environ.get("C2W_TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DB, reason="needs a migrated scratch database")
class TestStoring:
    @pytest.fixture
    async def db(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(TEST_DB)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    async def test_a_transcript_and_its_segments_are_written(self, db):
        from c2w.transcribe.service import store_transcript

        slug = f"tr-{uuid.uuid4().hex[:8]}"
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
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            tenant_id = (
                await s.execute(
                    text(
                        "INSERT INTO tenants (brand_id, name, slug) VALUES (:b,'t',:s) "
                        "RETURNING id"
                    ),
                    {"b": brand_id, "s": slug},
                )
            ).scalar_one()
            conn_id = (
                await s.execute(
                    text(
                        "INSERT INTO commpeak_connections (brand_id, tenant_id, name, "
                        "s3_bucket, s3_access_key_sealed, s3_secret_sealed) "
                        "VALUES (:b,:t,'c',:s,'x','x') RETURNING id"
                    ),
                    {"b": brand_id, "t": tenant_id, "s": slug},
                )
            ).scalar_one()
            rec_id = (
                await s.execute(
                    text(
                        "INSERT INTO recordings (brand_id, connection_id, tenant_id, "
                        "source_key, source_size, state) "
                        "VALUES (:b,:c,:t,'k',1,'AVAILABLE') RETURNING id"
                    ),
                    {"b": brand_id, "c": conn_id, "t": tenant_id},
                )
            ).scalar_one()

            class _Rec:
                id = rec_id
                cdr_id = None

            config = TranscribeSettings(
                enabled=True, engine="faster-whisper-local", model="tiny",
                primary_language="es — Spanish (Latin America)", diarize=False,
                word_timestamps=True, redact_numbers=True, only_longer_than=0,
                concurrency=1,
            )
            result = TranscriptResult(
                text="hola, su cuenta termina en 44",
                language="es", language_detected="es", confidence=0.8,
                duration_seconds=31.5,
                segments=[
                    Segment(seq=0, start_ms=0, end_ms=1500, text="hola,", confidence=0.9),
                    Segment(seq=1, start_ms=1500, end_ms=4000,
                            text="su cuenta termina en 44", confidence=0.7),
                ],
                engine="faster-whisper-local", model="tiny",
            )
            transcript_id = await store_transcript(s, _Rec(), brand_id, result, config)
            await s.commit()

            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            row = (
                await s.execute(
                    text(
                        "SELECT text, language, language_detected, redacted, "
                        "duration_seconds, engine FROM transcripts WHERE id = :i"
                    ),
                    {"i": transcript_id},
                )
            ).one()
            segments = (
                await s.execute(
                    text(
                        "SELECT seq, start_ms, text FROM transcript_segments "
                        "WHERE transcript_id = :i ORDER BY seq"
                    ),
                    {"i": transcript_id},
                )
            ).all()

        assert row[0] == "hola, su cuenta termina en 44"
        assert row[3] is True, "the redaction flag must record what was applied"
        # Whole seconds: the column is an integer, which is the right
        # resolution for "how long was this call" -- sub-second precision
        # would be recording noise as data.
        assert int(row[4]) == 31
        assert len(segments) == 2
        assert segments[1][2].endswith("44")

    async def test_transcribing_again_replaces_the_segments(self, db):
        """Two sets of timings for one recording is worse than either."""
        from c2w.transcribe.service import store_transcript

        slug = f"tr2-{uuid.uuid4().hex[:8]}"
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
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            tenant_id = (
                await s.execute(
                    text(
                        "INSERT INTO tenants (brand_id, name, slug) VALUES (:b,'t',:s) "
                        "RETURNING id"
                    ),
                    {"b": brand_id, "s": slug},
                )
            ).scalar_one()
            conn_id = (
                await s.execute(
                    text(
                        "INSERT INTO commpeak_connections (brand_id, tenant_id, name, "
                        "s3_bucket, s3_access_key_sealed, s3_secret_sealed) "
                        "VALUES (:b,:t,'c',:s,'x','x') RETURNING id"
                    ),
                    {"b": brand_id, "t": tenant_id, "s": slug},
                )
            ).scalar_one()
            rec_id = (
                await s.execute(
                    text(
                        "INSERT INTO recordings (brand_id, connection_id, tenant_id, "
                        "source_key, source_size, state) "
                        "VALUES (:b,:c,:t,'k',1,'AVAILABLE') RETURNING id"
                    ),
                    {"b": brand_id, "c": conn_id, "t": tenant_id},
                )
            ).scalar_one()

            class _Rec:
                id = rec_id
                cdr_id = None

            config = TranscribeSettings(
                enabled=True, engine="faster-whisper-local", model="tiny",
                primary_language="auto-detect", diarize=False, word_timestamps=True,
                redact_numbers=False, only_longer_than=0, concurrency=1,
            )
            first = TranscriptResult(
                text="one", segments=[Segment(0, 0, 100, "one")],
                engine="faster-whisper-local", model="tiny",
            )
            second = TranscriptResult(
                text="two", segments=[Segment(0, 0, 200, "two")],
                engine="faster-whisper-local", model="base",
            )
            tid = await store_transcript(s, _Rec(), brand_id, first, config)
            again = await store_transcript(s, _Rec(), brand_id, second, config)
            await s.commit()
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            rows = (
                await s.execute(
                    text("SELECT text FROM transcript_segments WHERE transcript_id = :i"),
                    {"i": again},
                )
            ).scalars().all()
            body = (
                await s.execute(
                    text("SELECT text, model FROM transcripts WHERE id = :i"), {"i": again}
                )
            ).one()

        assert tid == again, "the same recording must not get two transcript rows"
        assert rows == ["two"]
        assert body == ("two", "base")


@pytest.mark.skipif(not TEST_DB, reason="needs a migrated scratch database")
class TestOrganisationsStaySeparate:
    """One organisation's calls must never be transcribed into another's.

    This is a regression test for a leak that reached production. The
    transcriber runs on `platform_session()`, whose role has `BYPASSRLS`
    because the workers legitimately read across organisations -- so the
    `set_config('c2w.brand_id', ...)` that every other scoped query relies on
    does nothing at all on that connection. The pending-work query trusted it
    anyway, so the first organisation's pass claimed the newest recordings on
    the whole estate, transcribed them under its own settings, and wrote the
    text into its own partition.

    Six of one broker's calls were filed under the other's before it was
    caught. Both halves of the fix are asserted here: the query filters on
    `brand_id` itself, and the service refuses a recording that is not the
    organisation's even if something hands it one.
    """

    @pytest.fixture
    async def db(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(TEST_DB)
        yield async_sessionmaker(engine, expire_on_commit=False)
        await engine.dispose()

    async def _brand_with_a_recording(self, s) -> tuple[int, int]:
        """A brand, its partitions, and one archived recording. Returns both ids."""
        slug = f"iso-{uuid.uuid4().hex[:8]}"
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
        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )
        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (brand_id, name, slug) VALUES (:b,'t',:s) "
                    "RETURNING id"
                ),
                {"b": brand_id, "s": slug},
            )
        ).scalar_one()
        conn_id = (
            await s.execute(
                text(
                    "INSERT INTO commpeak_connections (brand_id, tenant_id, name, "
                    "s3_endpoint, s3_region, s3_bucket, s3_access_key_sealed, "
                    "s3_secret_sealed) VALUES (:b,:t,:n,'http://x','r',:s,'x','x') "
                    "RETURNING id"
                ),
                {"b": brand_id, "t": tenant_id, "n": slug, "s": slug},
            )
        ).scalar_one()
        # An archive to have been copied *to*. The pending query requires both
        # `destination_id` and `destination_key`, because a row with only one
        # of them can never be read and would be handed back on every pass.
        dest_id = (
            await s.execute(
                text(
                    "INSERT INTO storage_destinations (brand_id, name, endpoint, "
                    "region, bucket, access_key_sealed, secret_sealed) "
                    "VALUES (:b,:n,'http://x','r',:s,'x','x') RETURNING id"
                ),
                {"b": brand_id, "n": slug, "s": slug},
            )
        ).scalar_one()
        rec_id = (
            await s.execute(
                text(
                    "INSERT INTO recordings (brand_id, connection_id, tenant_id, "
                    "source_key, source_size, state, destination_id, destination_key, "
                    "started_at) "
                    "VALUES (:b,:c,:t,'k',1,'AVAILABLE',:d,'archived/k', now()) "
                    "RETURNING id"
                ),
                {"b": brand_id, "c": conn_id, "t": tenant_id, "d": dest_id},
            )
        ).scalar_one()
        await s.commit()
        return brand_id, rec_id

    async def test_a_pass_never_claims_another_organisations_recording(self, db, monkeypatch):
        """The exact query the transcriber runs, against two organisations."""
        import c2w.workers.transcriber as transcriber

        async with db() as s:
            first, first_rec = await self._brand_with_a_recording(s)
            second, second_rec = await self._brand_with_a_recording(s)

        # `_pending_recordings` opens its own platform session; point it at the
        # scratch database. This is the BYPASSRLS path in production, and the
        # scratch role is the same one the migrations run as -- so if the query
        # leaned on row-level security, this test would pass for the wrong
        # reason. It asserts on ownership rather than on row counts for that
        # reason too.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(TEST_DB)
        monkeypatch.setattr(
            transcriber, "platform_session", async_sessionmaker(engine, expire_on_commit=False)
        )
        try:
            claimed_first = await transcriber._pending_recordings(first, limit=50)
            claimed_second = await transcriber._pending_recordings(second, limit=50)
        finally:
            await engine.dispose()

        assert first_rec in claimed_first, "an organisation must see its own recording"
        assert second_rec in claimed_second
        assert second_rec not in claimed_first, (
            "one organisation's pass claimed another organisation's recording"
        )
        assert first_rec not in claimed_second

    async def test_the_service_refuses_a_recording_from_another_organisation(self, db):
        """Defence in depth: the guard holds even if the query is wrong again."""
        from c2w.transcribe.service import transcribe_recording

        async with db() as s:
            owner, rec_id = await self._brand_with_a_recording(s)
            other, _ = await self._brand_with_a_recording(s)

            config = TranscribeSettings(
                enabled=True, engine="faster-whisper-local", model="tiny",
                primary_language="auto-detect", diarize=False, word_timestamps=True,
                redact_numbers=False, only_longer_than=0, concurrency=1,
            )

            class _NeverRuns:
                name = "should-not-be-reached"

                async def transcribe(self, *a, **k):  # pragma: no cover
                    raise AssertionError("the audio must not even be fetched")

            # Scoped to the *owner*, so the row is genuinely visible and the
            # guard is what stops this rather than row-level security. That
            # models production, where the connection has BYPASSRLS and every
            # row is visible whatever the scope says -- scoping to `other`
            # here would hide the recording on the scratch role and the test
            # would pass without the guard existing at all.
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(owner)}
            )
            # Raises rather than returning None: "nothing to do" would let a
            # broken query quietly transcribe nothing for ever.
            with pytest.raises(ValueError, match="belongs to organisation"):
                await transcribe_recording(
                    s, rec_id, brand_id=other, engine=_NeverRuns(), config=config
                )

        assert owner != other
