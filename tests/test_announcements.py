"""Telling a conversation from a recording of one.

The phrases here are real: each appeared word for word in more than a hundred
calls in the first 30,000 transcripts.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.transcribe.announcements import judge, normalise
from c2w.transcribe.base import Segment

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")


def _segs(*parts: tuple[float, float, str]) -> list[Segment]:
    return [
        Segment(seq=i, start_ms=int(a * 1000), end_ms=int(b * 1000), text=t)
        for i, (a, b, t) in enumerate(parts)
    ]


async def _judge(segments, *, repeats=0):
    # No fingerprints in play, so no database is consulted.
    return await judge(None, 1, segments, repeats=repeats, min_speech_seconds=5, learn=False)


class TestKnownPhrases:
    async def test_a_carrier_voicemail_is_skipped(self):
        verdict = await _judge(
            _segs(
                (0, 6, "Bienvenido al servicio de buzón de voz."),
                (6, 9, "Después del tono, grabe su mensaje."),
            )
        )
        assert verdict.reason.startswith("skipped: recorded announcement")

    async def test_a_brazilian_mailbox_is_skipped(self):
        verdict = await _judge(_segs((0, 5, "Deixe a sua mensagem na caixa postal.")))
        assert verdict.reason.startswith("skipped: recorded announcement")

    async def test_a_busy_message_is_skipped(self):
        verdict = await _judge(_segs((0, 4, "El número que usted marcó está ocupado.")))
        assert verdict.reason.startswith("skipped: recorded announcement")

    async def test_an_agent_saying_hello_to_a_mailbox_is_still_skipped(self):
        """A couple of seconds of "¿hola? ¿aló?" over a voicemail greeting is
        not a conversation."""
        verdict = await _judge(
            _segs(
                (0, 6, "Bienvenido a Claro, después del tono tu llamada será cobrada."),
                (7, 8, "¿Hola?"),
                (9, 10, "¿Aló?"),
            )
        )
        assert verdict.reason is not None
        assert verdict.conversation_seconds == 2.0


class TestMusic:
    @pytest.mark.parametrize(
        "caption",
        [
            "♪ ♪ ♪",
            "[Música]",
            "Subtítulos realizados por la comunidad de Amara.org",
            "¡Gracias por ver el video!",
            "Thank you for watching.",
        ],
    )
    async def test_music_and_whispers_captions_are_not_speech(self, caption):
        verdict = await _judge(_segs((0, 40, caption)))
        assert verdict.reason == "skipped: music on hold"


class TestConversationsAreKept:
    async def test_a_call_that_starts_at_the_menu_is_kept(self):
        """The menu greeting does not make a real call a recording."""
        verdict = await _judge(
            _segs(
                (0, 8, "Después del tono, grabe su mensaje."),
                (9, 20, "Buenas tardes, le llamo por su cuenta de inversión."),
                (20, 31, "Sí, dígame, ¿qué necesita?"),
            )
        )
        assert verdict.reason is None
        assert verdict.conversation_seconds == 22.0

    async def test_ordinary_talk_is_left_to_the_other_rules(self):
        """No recording and no music: nothing here to judge."""
        verdict = await _judge(_segs((0, 2, "Sí."), (2, 3, "Ok.")))
        assert verdict.reason is None

    def test_normalising_ignores_case_accents_and_punctuation(self):
        assert normalise("¡Después del TONO, grabe!") == "despues del tono grabe"


@pytest.mark.skipif(not TEST_DB, reason="needs a migrated scratch database")
class TestLearning:
    @pytest.fixture
    async def scoped(self):
        engine = create_async_engine(TEST_DB)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        brands = []
        async with factory() as s:
            for _ in range(2):
                slug = f"ann-{uuid.uuid4().hex[:8]}"
                brands.append(
                    (
                        await s.execute(
                            text("INSERT INTO brands (name, slug) VALUES (:n,:s) RETURNING id"),
                            {"n": slug, "s": slug},
                        )
                    ).scalar_one()
                )
            await s.commit()

        async def session_for(brand_id):
            s = factory()
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
            )
            return s

        yield brands, session_for
        await engine.dispose()

    async def test_a_repeated_sentence_becomes_a_recording(self, scoped):
        (brand, _), session_for = scoped
        prompt = "Hola y gracias por llamarnos, si conoce la extensión márquela ahora."
        s = await session_for(brand)
        for _ in range(3):
            first = await judge(s, brand, _segs((0, 7, prompt)), repeats=3, min_speech_seconds=5)
        # The third call is the one that reaches the threshold.
        assert first.reason.startswith("skipped: recorded announcement")
        await s.commit()
        await s.close()

    async def test_one_organisation_does_not_learn_from_another(self, scoped):
        (a, b), session_for = scoped
        prompt = "Bienvenido a la central, por favor espere a ser atendido por un agente."
        s = await session_for(a)
        for _ in range(5):
            await judge(s, a, _segs((0, 7, prompt)), repeats=3, min_speech_seconds=5)
        await s.commit()
        await s.close()

        s = await session_for(b)
        verdict = await judge(
            s, b, _segs((0, 7, prompt)), repeats=3, min_speech_seconds=5, learn=False
        )
        assert verdict.reason is None, "B must not see A's fingerprints"
        rows = (
            await s.execute(
                text("SELECT count(*) FROM transcribe_phrases WHERE brand_id = :a"), {"a": a}
            )
        ).scalar_one()
        assert rows == 0
        await s.close()
