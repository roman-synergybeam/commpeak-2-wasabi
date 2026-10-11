"""Choosing a recogniser, running it over a recording, and storing the result.

The engine choice is a setting with six options and two implementations, so
this is also the one place that says clearly which of them can actually run --
rather than letting somebody select "azure-speech", see no error, and wonder
why nothing happens.

Audio is fetched to a temporary file and deleted immediately afterwards.
Streaming it into the recogniser would be nicer, but faster-whisper wants a
path, and a call recording is megabytes rather than gigabytes -- unlike the
transfer engine, where staging on disk was never an option.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.logging import get_logger
from c2w.settings import settings_service
from c2w.transcribe.base import EngineUnavailable, Transcriber, TranscriptResult

__all__ = [
    "TranscribeSettings",
    "build_engine",
    "load_settings",
    "store_transcript",
    "transcribe_recording",
]

log = get_logger(__name__)

#: Only these two run here. The rest are in the settings menu because a
#: customer may require them, and each would be an integration of its own with
#: its own credentials and its own data-residency question.
IMPLEMENTED_ENGINES = {"whisper-local", "faster-whisper-local"}

#: Upper bound on fetching and recognising one recording. Neither the archive
#: download nor the recogniser had one, and on 2026-10-07 one of them simply
#: never returned: the transcriber stopped, silently, for three days.
TRANSCRIBE_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True, slots=True)
class TranscribeSettings:
    enabled: bool
    engine: str
    model: str
    primary_language: str
    diarize: bool
    word_timestamps: bool
    redact_numbers: bool
    only_longer_than: int
    concurrency: int
    min_speech_seconds: int = 0


async def load_settings(
    session: AsyncSession, *, brand_id: int | None = None
) -> TranscribeSettings:
    get = settings_service
    return TranscribeSettings(
        enabled=await get.get_bool(session, "transcribe.enabled", brand_id=brand_id),
        engine=await get.get_str(session, "transcribe.engine", brand_id=brand_id),
        model=await get.get_str(session, "transcribe.model", brand_id=brand_id),
        primary_language=await get.get_str(
            session, "transcribe.primary_language", brand_id=brand_id
        ),
        diarize=await get.get_bool(session, "transcribe.diarize", brand_id=brand_id),
        word_timestamps=await get.get_bool(
            session, "transcribe.word_timestamps", brand_id=brand_id
        ),
        redact_numbers=await get.get_bool(
            session, "transcribe.redact_numbers", brand_id=brand_id
        ),
        only_longer_than=await get.get_int(
            session, "transcribe.only_longer_than", brand_id=brand_id
        ),
        concurrency=await get.get_int(session, "transcribe.concurrency", brand_id=brand_id),
        min_speech_seconds=await get.get_int(
            session, "transcribe.min_speech_seconds", brand_id=brand_id
        ),
    )


def skip_reason(result: TranscriptResult, config: TranscribeSettings) -> str | None:
    """Why this result should be recorded as skipped, or None to store it.

    One rule for every recogniser, so a transcript from a remote agent is held
    to exactly what the server's own would be. The reason is written to
    `failed_reason`, and the row is what stops the recording being handed out
    again.
    """
    duration = result.duration_seconds or 0
    if (
        config.min_speech_seconds
        and result.speech_seconds is not None
        and result.speech_seconds < config.min_speech_seconds
    ):
        return (
            f"skipped: no speech -- {result.speech_seconds:.0f}s of speech in "
            f"{duration:.0f}s of audio (hold music, ringing or silence)"
        )
    if config.only_longer_than and duration < config.only_longer_than:
        return (
            f"skipped: {duration:.0f}s of audio is under the "
            f"{config.only_longer_than}s minimum in settings"
        )
    if not result.text.strip():
        return "skipped: no speech -- the recogniser heard no words"
    return None


def build_engine(config: TranscribeSettings) -> Transcriber:
    """The recogniser for these settings, or a clear refusal."""
    if config.engine not in IMPLEMENTED_ENGINES:
        raise EngineUnavailable(
            f"the {config.engine!r} recogniser is not built into this system yet; "
            "choose 'faster-whisper-local' to run on this server"
        )
    from c2w.transcribe.whisper_local import WhisperLocal

    return WhisperLocal(
        model=config.model,
        redact=config.redact_numbers,
    )


async def transcribe_recording(
    session: AsyncSession,
    recording_id: int,
    *,
    brand_id: int,
    engine: Transcriber,
    config: TranscribeSettings,
) -> TranscriptResult | None:
    """Fetch one recording's audio, recognise it, and store the transcript.

    Returns None when there is nothing to do -- too short, no archived copy
    yet, or already transcribed -- because those are ordinary outcomes and not
    failures to alert on.
    """
    from sqlalchemy import select

    from c2w.db.models.core import Recording, StorageDestination
    from c2w.storage.factory import open_destination

    recording = (
        await session.execute(select(Recording).where(Recording.id == recording_id))
    ).scalar_one_or_none()
    if recording is None:
        return None

    # Refuse rather than trust the caller's scope. These run on a
    # `BYPASSRLS` connection, so the lookup above is *not* confined to one
    # organisation however `c2w.brand_id` is set -- and everything below
    # writes `brand_id` from the argument, not from the row. Without this
    # check, being handed the wrong id silently files one customer's
    # conversation under another customer, in that customer's partition,
    # readable from that customer's console.
    #
    # It is a guard on an invariant the caller is supposed to hold, so it
    # raises rather than returning None: "nothing to do" would let a broken
    # query quietly transcribe nothing for ever.
    if recording.brand_id != brand_id:
        raise ValueError(
            f"recording {recording_id} belongs to organisation "
            f"{recording.brand_id}, not {brand_id}"
        )

    already = (
        await session.execute(
            text("SELECT 1 FROM transcripts WHERE recording_id = :r AND text IS NOT NULL"),
            {"r": recording_id},
        )
    ).first()
    if already:
        return None

    if not recording.destination_id or not recording.destination_key:
        # Nothing to read: the audio is still only on CommPeak, and CommPeak is
        # read-only for *this* purpose too -- transcribing from the source
        # would mean pulling 13.9 TB through here twice.
        return None

    destination = (
        await session.execute(
            select(StorageDestination).where(
                StorageDestination.id == recording.destination_id
            )
        )
    ).scalar_one_or_none()
    if destination is None:
        return None

    suffix = os.path.splitext(recording.destination_key)[1] or ".flac"
    handle, path = tempfile.mkstemp(prefix="c2w-tr-", suffix=suffix)
    os.close(handle)
    try:
        store = await open_destination(session, destination)
        # End the transaction before the slow part. The download and the
        # recogniser take minutes, and a transaction left open across them is
        # a snapshot the whole database has to keep: one that hung held it for
        # three and a half days, while every full scan elsewhere slowed past
        # its timeout. The writes below set the scope again for themselves.
        await session.commit()

        async def fetch_and_recognise() -> TranscriptResult:
            async with store:
                # Buffered, then written in a worker thread: a synchronous
                # write inside the async loop blocks every other request in
                # the process for the length of the download. One call
                # recording is a few megabytes, so holding it is cheap --
                # unlike the transfer engine, where 13.9 TB through an 84 GB
                # volume rules staging out entirely.
                buffer = bytearray()
                async for chunk in store.open_stream(recording.destination_key):
                    buffer.extend(chunk)
            await asyncio.to_thread(_write_bytes, path, bytes(buffer))
            return await engine.transcribe(
                path,
                language=config.primary_language,
                word_timestamps=config.word_timestamps,
                min_speech_seconds=config.min_speech_seconds,
            )

        result = await asyncio.wait_for(
            fetch_and_recognise(), timeout=TRANSCRIBE_TIMEOUT_SECONDS
        )
    except EngineUnavailable:
        raise
    except Exception as exc:
        # `str()` of a timeout is empty, so the type goes in too.
        error = f"{type(exc).__name__}: {exc}"[:400]
        log.warning("transcribe.failed", recording_id=recording_id, error=error[:200])
        await _record_failure(session, recording, brand_id, error, config)
        return None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)

    reason = skip_reason(result, config)
    if reason is not None:
        log.info(
            "transcribe.skipped",
            recording_id=recording_id,
            seconds=result.duration_seconds,
            speech_seconds=result.speech_seconds,
        )
        # Recorded rather than simply skipped. The pending query finds work by
        # "archived recording with no transcripts row", so a recording that is
        # returned and then dropped is returned again on the very next pass --
        # which, now that the transcriber is a loop of its own rather than a
        # step in a thirty-second tick, means downloading and recognising the
        # same three-second voicemail continuously. The row is what makes the
        # decision stick.
        await _record_failure(session, recording, brand_id, reason, config)
        return None

    await store_transcript(session, recording, brand_id, result, config)
    return result


async def _set_scope(session: AsyncSession, brand_id: int) -> None:
    """Set the row-security scope for the transaction about to write.

    `transcribe_recording` commits before the download, which ends the
    transaction-local scope its caller set, so each write sets it again. It is
    what the policies' `WITH CHECK` files the rows under.
    """
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
    )


async def _record_failure(
    session: AsyncSession, recording, brand_id: int, reason: str, config: TranscribeSettings
) -> None:
    """Remember that this one failed, so it is not retried for ever in a loop."""
    await _set_scope(session, brand_id)
    await session.execute(
        text(
            "INSERT INTO transcripts (brand_id, recording_id, cdr_id, engine, model, "
            "failed_reason) VALUES (:b, :r, :c, :e, :m, :reason) "
            "ON CONFLICT (brand_id, recording_id) DO UPDATE SET "
            "failed_reason = EXCLUDED.failed_reason, updated_at = now()"
        ),
        {
            "b": brand_id,
            "r": recording.id,
            "c": recording.cdr_id,
            "e": config.engine,
            "m": config.model,
            "reason": reason,
        },
    )


async def store_transcript(
    session: AsyncSession,
    recording,
    brand_id: int,
    result: TranscriptResult,
    config: TranscribeSettings,
) -> int:
    """Write the transcript and its segments.

    Segments are replaced rather than appended when a recording is transcribed
    again: two sets of timings for one recording is worse than either.
    """
    await _set_scope(session, brand_id)
    transcript_id = (
        await session.execute(
            text(
                "INSERT INTO transcripts (brand_id, recording_id, cdr_id, engine, model, "
                "language, language_detected, confidence, duration_seconds, text, "
                "redacted, diarized, speaker_count, engine_detail, failed_reason) "
                "VALUES (:b, :r, :c, :engine, :model, :language, :detected, :confidence, "
                ":duration, :text, :redacted, :diarized, :speakers, "
                "CAST(:detail AS jsonb), NULL) "
                "ON CONFLICT (brand_id, recording_id) DO UPDATE SET "
                "engine = EXCLUDED.engine, model = EXCLUDED.model, "
                "language = EXCLUDED.language, "
                "language_detected = EXCLUDED.language_detected, "
                "confidence = EXCLUDED.confidence, "
                "duration_seconds = EXCLUDED.duration_seconds, text = EXCLUDED.text, "
                "redacted = EXCLUDED.redacted, diarized = EXCLUDED.diarized, "
                "speaker_count = EXCLUDED.speaker_count, "
                "engine_detail = EXCLUDED.engine_detail, failed_reason = NULL, "
                "updated_at = now() "
                "RETURNING id"
            ),
            {
                "b": brand_id,
                "r": recording.id,
                "c": recording.cdr_id,
                "engine": result.engine or config.engine,
                "model": result.model or config.model,
                "language": result.language,
                "detected": result.language_detected,
                "confidence": result.confidence,
                "duration": result.duration_seconds,
                "text": result.text,
                "redacted": config.redact_numbers,
                "diarized": bool(result.speaker_count),
                "speakers": result.speaker_count,
                "detail": _json(result.engine_detail),
            },
        )
    ).scalar_one()

    await session.execute(
        text("DELETE FROM transcript_segments WHERE transcript_id = :t"),
        {"t": transcript_id},
    )
    for segment in result.segments:
        await session.execute(
            text(
                "INSERT INTO transcript_segments (brand_id, transcript_id, seq, "
                "start_ms, end_ms, speaker, text, confidence) "
                "VALUES (:b, :t, :seq, :start, :end, :speaker, :text, :confidence)"
            ),
            {
                "b": brand_id,
                "t": transcript_id,
                "seq": segment.seq,
                "start": segment.start_ms,
                "end": segment.end_ms,
                "speaker": segment.speaker,
                "text": segment.text,
                "confidence": segment.confidence,
            },
        )
    await session.flush()
    return int(transcript_id)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _json(value: dict) -> str:
    import json

    return json.dumps(value, default=str)
