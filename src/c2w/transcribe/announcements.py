"""Telling a conversation from a recording of one.

Three kinds of audio are not conversations and are ignored:

* **silence and ringing** -- caught before recognition, by the speech detector
  (`speech_seconds`, see `skip_reason`);
* **music on hold** -- Whisper renders it as "♪", as "[Música]", or as the
  filler text it was trained on from subtitled video ("Subtítulos realizados
  por la comunidad de Amara.org", "thank you for watching"), none of it said;
* **recorded announcements** -- carrier voicemail greetings, "the number you
  have dialled is busy", the organisation's own phone menu.

Announcements are found two ways. A short list of phrases no person says in a
sales call ("buzón de voz", "deixe a sua mensagem na caixa postal", "after the
tone") catches the common ones from the first call. And every sentence of
five or more words is fingerprinted and counted per organisation: a sentence
that turns up word for word in many unrelated recordings is a recording.
On the first 30,000 transcripts that found InterMagnum's phone-menu greeting in
414 calls and Claro's voicemail in 184; it needs no list per carrier or per
language, and it learns a new carrier's prompt by itself.

**Nothing is removed from a call that is kept.** A sales script is also the
same sentence in many calls, so a fingerprint must never be read as "delete
this line". Announcement segments only decide whether a call had a
conversation in it: a call whose speech, besides recordings and music, is
under `transcribe.min_speech_seconds` is skipped; one with a real exchange
keeps its whole transcript, menu greeting and all.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.transcribe.base import Segment

__all__ = ["Verdict", "judge", "normalise", "phrase_hash", "reclassify"]

log = get_logger(__name__)

#: Shorter sentences are ordinary conversation however often they repeat:
#: "sí", "ok", "gracias", "buenas tardes" are each in hundreds of calls.
MIN_WORDS = 5

#: Phrases a recording says and a person in a sales call does not. Matched on
#: the normalised text (lower case, no accents, no punctuation).
_ANNOUNCEMENT = re.compile(
    r"buzon de voz|correo de voz|caixa postal|"
    r"despues del? (tono|senal|pitido)|depois do (sinal|bipe)|"
    r"deje su mensaje|grabe su mensaje|deixe (a )?sua mensagem|grave (a )?sua mensagem|"
    r"el numero (que usted )?(marco|ha marcado|solicitado|que marco)|"
    r"o numero (que voce )?(ligou|discou|chamou|chamado)|"
    r"fuera de (servicio|cobertura)|fuera del area|"
    r"(llamada )?sera cobrada|se cobrara al terminar|"
    r"after the (tone|beep)|leave (a|your) message|"
    r"the number you have (dialed|dialled|called)|"
    r"voice ?mail|mailbox"
)

#: Music, and what Whisper writes when it hears music or nothing: captions it
#: learnt from subtitled video, never said on a phone call.
_MUSIC = re.compile(
    r"^\W*$|^\W*(musica|music|musique|instrumental)\W*$|"
    r"amara ?org|subtitulos (realizados|por)|legendas pela|"
    r"gracias por ver|obrigad[oa] por assistir|thanks? (you )?for watching|"
    r"suscribete|subscribe"
)


@dataclass(slots=True)
class Verdict:
    conversation_seconds: float
    announcement_seconds: float
    music_seconds: float
    reason: str | None


def normalise(sentence: str) -> str:
    """Lower case, accents and punctuation gone, spaces collapsed."""
    folded = unicodedata.normalize("NFKD", sentence.lower())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w♪♫]+", " ", plain).split())


def phrase_hash(normalised: str) -> str:
    return hashlib.sha1(normalised.encode()).hexdigest()[:16]  # noqa: S324 - a fingerprint


async def judge(
    session: AsyncSession,
    brand_id: int,
    segments: list[Segment],
    *,
    repeats: int,
    min_speech_seconds: int,
    learn: bool = True,
) -> Verdict:
    """Was there a conversation besides recordings and music?

    With `learn`, this recording's sentences are counted first -- the
    voicemail calls are exactly what teaches the fingerprints, so they count
    even when they are about to be skipped.
    """
    music = announcement = conversation = 0.0
    keyed: list[tuple[Segment, str, str | None]] = []
    for seg in segments:
        norm = normalise(seg.text)
        key = phrase_hash(norm) if len(norm.split()) >= MIN_WORDS else None
        keyed.append((seg, norm, key))

    keys = sorted({k for _, _, k in keyed if k})
    if learn and keys:
        samples = {k: s.text.strip()[:300] for s, _, k in keyed if k}
        for key in keys:
            # `brand_id = :b` written out: the local transcriber runs this on
            # the BYPASSRLS role, where the policy is not consulted.
            await session.execute(
                text(
                    "INSERT INTO transcribe_phrases (brand_id, phrase_hash, sample, calls) "
                    "VALUES (:b, :h, :s, 1) "
                    "ON CONFLICT (brand_id, phrase_hash) DO UPDATE SET "
                    "calls = transcribe_phrases.calls + 1, updated_at = now()"
                ),
                {"b": brand_id, "h": key, "s": samples[key]},
            )

    seen: dict[str, int] = {}
    if repeats and keys:
        seen = dict(
            (
                await session.execute(
                    text(
                        "SELECT phrase_hash, calls FROM transcribe_phrases "
                        "WHERE brand_id = :b AND phrase_hash = ANY(:h)"
                    ),
                    {"b": brand_id, "h": keys},
                )
            ).all()
        )

    for seg, norm, key in keyed:
        seconds = max(0.0, (seg.end_ms - seg.start_ms) / 1000)
        if _MUSIC.search(norm):
            music += seconds
        elif _ANNOUNCEMENT.search(norm) or (repeats and key and seen.get(key, 0) >= repeats):
            announcement += seconds
        else:
            conversation += seconds

    reason = None
    if (music or announcement) and conversation < min_speech_seconds:
        reason = (
            "skipped: music on hold"
            if music > announcement
            else "skipped: recorded announcement -- voicemail, carrier or phone-menu message"
        )
    return Verdict(
        conversation_seconds=round(conversation, 1),
        announcement_seconds=round(announcement, 1),
        music_seconds=round(music, 1),
        reason=reason,
    )


async def reclassify(
    session: AsyncSession,
    brand_id: int,
    *,
    repeats: int,
    min_speech_seconds: int,
    since=None,
    rebuild: bool = False,
) -> dict[str, int]:
    """Apply the rule to transcripts already stored.

    Fingerprints only become announcements after enough calls, so the first
    calls carrying a new prompt are stored as conversations; this catches
    them up. With `rebuild`, the counts are rebuilt from every stored segment
    first -- for the transcripts made before fingerprinting existed.
    Runs in a brand-scoped session; `brand_id = :b` is written out anyway.
    """
    rows = (
        await session.execute(
            text(
                "SELECT s.transcript_id, s.seq, s.start_ms, s.end_ms, s.text "
                "FROM transcript_segments s JOIN transcripts t "
                "  ON t.id = s.transcript_id AND t.brand_id = s.brand_id "
                "WHERE s.brand_id = :b AND t.text IS NOT NULL "
                "  AND (CAST(:since AS timestamptz) IS NULL OR t.updated_at >= :since) "
                "ORDER BY s.transcript_id, s.seq"
            ),
            {"b": brand_id, "since": since},
        )
    ).all()
    by_transcript: dict[int, list[Segment]] = {}
    for tid, seq, start, end, sentence in rows:
        by_transcript.setdefault(tid, []).append(
            Segment(seq=seq, start_ms=start, end_ms=end, text=sentence)
        )

    if rebuild:
        counts: dict[str, int] = {}
        samples: dict[str, str] = {}
        for segments in by_transcript.values():
            for key in {
                phrase_hash(n)
                for n in (normalise(s.text) for s in segments)
                if len(n.split()) >= MIN_WORDS
            }:
                counts[key] = counts.get(key, 0) + 1
            for s in segments:
                n = normalise(s.text)
                if len(n.split()) >= MIN_WORDS:
                    samples.setdefault(phrase_hash(n), s.text.strip()[:300])
        await session.execute(
            text("DELETE FROM transcribe_phrases WHERE brand_id = :b"), {"b": brand_id}
        )
        if counts:
            await session.execute(
                text(
                    "INSERT INTO transcribe_phrases (brand_id, phrase_hash, sample, calls) "
                    "VALUES (:b, :h, :s, :c)"
                ),
                [
                    {"b": brand_id, "h": k, "s": samples[k], "c": c}
                    for k, c in counts.items()
                ],
            )

    skipped = 0
    for tid, segments in by_transcript.items():
        verdict = await judge(
            session,
            brand_id,
            segments,
            repeats=repeats,
            min_speech_seconds=min_speech_seconds,
            learn=False,
        )
        if verdict.reason is None:
            continue
        # `updated_at` deliberately left alone: the progress page counts what
        # was dealt with per hour, and a reclassification is not new work.
        await session.execute(
            text("DELETE FROM transcript_segments WHERE brand_id = :b AND transcript_id = :t"),
            {"b": brand_id, "t": tid},
        )
        await session.execute(
            text(
                "UPDATE transcripts SET text = NULL, failed_reason = :r "
                "WHERE brand_id = :b AND id = :t"
            ),
            {"b": brand_id, "t": tid, "r": verdict.reason},
        )
        skipped += 1
    return {"checked": len(by_transcript), "skipped": skipped}
