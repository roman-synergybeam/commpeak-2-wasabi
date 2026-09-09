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
