"""Remote transcription agents: work handed to a GPU machine elsewhere.

The server has eight cores and no GPU -- about one call a minute against an
archive of twenty-four million. A machine with a GPU does the same work many
times faster, and this module is the server's side of lending it the work
without lending it the archive:

* **It never holds a credential for the archive.** Each job carries a presigned
  URL for one object, expiring with the lease. A compromised agent can read
  what it was handed in the last few minutes and nothing else.
* **The server decides which organisation a job belongs to,** and every step
  runs in a `brand_session`, under the same row-level security as a user
  request. The agent sees an opaque job id; the organisation is recovered from
  the lease it names, never from anything the agent says.
* **The server judges the result.** Redaction, the minimum-speech rule and the
  minimum-length rule are applied here, to what the agent sends back, so a
  transcript from a remote machine is held to exactly what the server's own
  recogniser would be.

The agent works oldest first and the server's own transcriber newest first, so
the two meet in the middle instead of competing for the same calls; a lease
keeps them apart where they do meet.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.db.models.core import Brand, Recording, StorageDestination
from c2w.db.session import brand_session
from c2w.logging import get_logger
from c2w.transcribe import announcements
from c2w.transcribe.base import Segment, TranscriptResult, redact_digits
from c2w.transcribe.service import (
    _record_failure,
    load_settings,
    skip_reason,
    store_transcript,
)
from c2w.transcribe.whisper_local import _language_code

__all__ = [
    "LEASE_SECONDS",
    "Agent",
    "LeaseGone",
    "authenticate",
    "claim",
    "create_agent",
    "fail",
    "hash_token",
    "note_detail",
    "progress",
    "revoke_agent",
    "submit",
]

log = get_logger(__name__)

#: How long an agent holds a job, and how long its download link lives. Long
#: enough for a slow download and a long call on a busy GPU; short enough that
#: a machine switched off mid-batch gives its work back the same hour.
LEASE_SECONDS = 20 * 60

#: The most jobs one claim hands out.
MAX_CLAIM = 32

#: Tokens carry a prefix so one pasted into the wrong place is recognisable,
#: and so a credential scan can be taught the shape.
TOKEN_PREFIX = "c2wa_"  # noqa: S105 - a prefix, not a credential

_PROGRESS_CACHE_SECONDS = 20.0
_progress_cache: tuple[float, list[dict]] | None = None


class LeaseGone(LookupError):
    """The job is not (or no longer) held by this agent."""


@dataclass(slots=True)
class Agent:
    id: int
    name: str


@dataclass(slots=True)
class AgentResult:
    """What an agent reports for one recording. Validated by the API layer."""

    segments: list[dict] = field(default_factory=list)
    language: str | None = None
    language_detected: str | None = None
    language_probability: float | None = None
    duration_seconds: float | None = None
    speech_seconds: float | None = None
    model: str = ""
    device: str = ""
    seconds_taken: float | None = None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_agent(session: AsyncSession, name: str) -> str:
    """Register a machine and return its token. Only the hash is kept."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    await session.execute(
        text("INSERT INTO transcribe_agents (name, token_hash) VALUES (:n, :h)"),
        {"n": name, "h": hash_token(token)},
    )
    return token


async def revoke_agent(session: AsyncSession, name: str) -> bool:
    revoked = await session.execute(
        text(
            "UPDATE transcribe_agents SET revoked_at = now() "
            "WHERE name = :n AND revoked_at IS NULL"
        ),
        {"n": name},
    )
    return bool(revoked.rowcount)


async def authenticate(session: AsyncSession, token: str) -> Agent | None:
    """The agent this token belongs to, or None. Records that it was seen."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = (
        await session.execute(
            text(
                "UPDATE transcribe_agents SET last_seen_at = now() "
                "WHERE token_hash = :h AND revoked_at IS NULL RETURNING id, name"
            ),
            {"h": hash_token(token)},
        )
    ).first()
    return Agent(id=int(row[0]), name=row[1]) if row else None


async def note_detail(session: AsyncSession, agent: Agent, detail: dict) -> None:
    """Keep what the machine last said about itself -- its GPU, its version."""
    await session.execute(
        text("UPDATE transcribe_agents SET last_detail = CAST(:d AS jsonb) WHERE id = :i"),
        {"d": _json(detail), "i": agent.id},
    )


async def claim(agent: Agent, *, limit: int, presign=None) -> list[dict]:
    """Hand out up to `limit` recordings, shared across the organisations.

    `presign` is injectable so tests need no archive; by default it opens the
    organisation's own destination and signs a GET for the one object.
    """
    presign = presign or _presign
    limit = max(1, min(limit, MAX_CLAIM))
    jobs: list[dict] = []

    from c2w.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        brands = (
            await session.execute(
                select(Brand.id).where(Brand.is_active.is_(True)).order_by(Brand.id)
            )
        ).scalars().all()

    # An even share each, so one organisation's backlog cannot starve the
    # other's. Whatever one leaves unused is not redistributed within a claim;
    # the next claim asks again.
    share = max(1, -(-limit // max(1, len(brands))))
    for brand_id in brands:
        if len(jobs) >= limit:
            break
        async with brand_session(brand_id) as session:
            jobs.extend(
                await _claim_for_brand(
                    session, agent, brand_id, min(share, limit - len(jobs)), presign
                )
            )
    return jobs


async def _claim_for_brand(
    session: AsyncSession, agent: Agent, brand_id: int, limit: int, presign
) -> list[dict]:
    config = await load_settings(session, brand_id=brand_id)
    if not config.enabled:
        return []

    # Leases that ran out go back to the pool -- and the cursor goes back to
    # the oldest of them, or the scan would never look behind it again.
    expired_from = (
        await session.execute(
            text(
                "WITH gone AS (DELETE FROM transcribe_leases "
                "  WHERE brand_id = :b AND expires_at < now() RETURNING started_at) "
                "SELECT min(started_at) FROM gone"
            ),
            {"b": brand_id},
        )
    ).scalar()
    if expired_from is not None:
        await _move_cursor(session, brand_id, expired_from, backwards=True)

    cursor = (
        await session.execute(
            text("SELECT started_from FROM transcribe_cursors WHERE brand_id = :b"),
            {"b": brand_id},
        )
    ).scalar()

    # `r.brand_id = :b` as well as RLS: belt and braces costs nothing here, and
    # the same query run on the platform role would otherwise see everything.
    rows = (
        await session.execute(
            text(
                "SELECT r.id, r.cdr_id, r.started_at, r.destination_id, "
                "       r.destination_key, c.bill_duration "
                "FROM recordings r "
                "LEFT JOIN cdrs c ON c.id = r.cdr_id AND c.brand_id = r.brand_id "
                "WHERE r.brand_id = :b "
                "  AND r.state IN ('AVAILABLE', 'SOURCE_DELETED') "
                "  AND r.destination_key IS NOT NULL "
                "  AND r.destination_id IS NOT NULL "
                "  AND r.started_at >= COALESCE(CAST(:cursor AS timestamptz), '-infinity') "
                "  AND NOT EXISTS (SELECT 1 FROM transcripts t "
                "                  WHERE t.brand_id = r.brand_id AND t.recording_id = r.id) "
                "  AND NOT EXISTS (SELECT 1 FROM transcribe_leases l "
                "                  WHERE l.brand_id = r.brand_id AND l.recording_id = r.id) "
                "ORDER BY r.started_at "
                "LIMIT :n"
            ),
            {"b": brand_id, "cursor": cursor, "n": limit},
        )
    ).all()
    if not rows:
        return []

    leased: list = []
    for row in rows:
        if row.bill_duration == 0:
            # CommPeak's own record says nobody spoke: cancelled, unanswered,
            # busy. Recorded as skipped without downloading anything.
            await _record_failure(
                session,
                row,
                brand_id,
                "skipped: not answered -- the call record shows no talk time",
                config,
            )
            continue
        token = secrets.token_urlsafe(24)
        taken = (
            await session.execute(
                text(
                    "INSERT INTO transcribe_leases "
                    "(brand_id, recording_id, agent_id, token, started_at, expires_at) "
                    "VALUES (:b, :r, :a, :t, :s, now() + make_interval(secs => :ttl)) "
                    "ON CONFLICT DO NOTHING RETURNING token"
                ),
                {
                    "b": brand_id, "r": row.id, "a": agent.id, "t": token,
                    "s": row.started_at, "ttl": LEASE_SECONDS,
                },
            )
        ).scalar()
        if taken:
            leased.append((row, token))

    await _move_cursor(session, brand_id, max(r.started_at for r in rows))
    if not leased:
        return []

    destinations = {
        d.id: d
        for d in (
            await session.execute(
                select(StorageDestination).where(
                    StorageDestination.brand_id == brand_id,
                    StorageDestination.id.in_({row.destination_id for row, _ in leased}),
                )
            )
        ).scalars()
    }
    urls = await presign(
        session,
        [(destinations[row.destination_id], row.destination_key) for row, _ in leased],
    )

    # The agent is about to download customer audio, which is media access
    # like a playback and is recorded like one: one row per organisation per
    # claim, rather than a row per file the size of the archive.
    await session.execute(
        text(
            "INSERT INTO audit_events (brand_id, actor_label, action, result, detail) "
            "VALUES (:b, :who, 'transcribe.agent_claim', 'SUCCESS', CAST(:d AS jsonb))"
        ),
        {
            "b": brand_id,
            "who": f"transcription agent {agent.name}",
            "d": _json({"recordings": [row.id for row, _ in leased]}),
        },
    )

    return [
        {
            "job_id": f"{brand_id}.{token}",
            "audio_url": url,
            "suffix": _suffix(row.destination_key),
            "language": _language_code(config.primary_language),
            "model": config.model,
            "word_timestamps": config.word_timestamps,
            "min_speech_seconds": config.min_speech_seconds,
            "only_longer_than": config.only_longer_than,
            "expires_in": LEASE_SECONDS,
        }
        for (row, token), url in zip(leased, urls, strict=True)
    ]


async def submit(agent: Agent, job_id: str, result: AgentResult) -> str:
    """Store what an agent heard. Returns "stored" or "skipped"."""
    brand_id, token = _parse_job(job_id)
    async with brand_session(brand_id) as session:
        recording_id = await _held_lease(session, agent, token)
        recording = (
            await session.execute(
                select(Recording).where(
                    Recording.brand_id == brand_id, Recording.id == recording_id
                )
            )
        ).scalar_one()
        config = await load_settings(session, brand_id=brand_id)

        segments = [
            Segment(
                seq=index,
                start_ms=int(float(seg.get("start", 0)) * 1000),
                end_ms=int(float(seg.get("end", 0)) * 1000),
                # Redacted here, on the server, whatever the agent did: the
                # setting is this organisation's and is not the agent's to keep.
                text=redact_digits(text_) if config.redact_numbers else text_,
                confidence=_bounded(seg.get("confidence")),
            )
            for index, seg in enumerate(result.segments)
            if (text_ := str(seg.get("text", "")).strip())
        ]
        confidences = [s.confidence for s in segments if s.confidence is not None]
        transcript = TranscriptResult(
            text=" ".join(s.text for s in segments),
            language=result.language,
            language_detected=result.language_detected,
            confidence=round(sum(confidences) / len(confidences), 4) if confidences else None,
            duration_seconds=result.duration_seconds,
            speech_seconds=result.speech_seconds,
            segments=segments,
            engine="faster-whisper-agent",
            model=result.model or config.model,
            engine_detail={
                "agent": agent.name,
                "device": result.device,
                "language_probability": result.language_probability,
                "speech_seconds": result.speech_seconds,
                "seconds_taken": result.seconds_taken,
                "vad_filter": True,
            },
        )

        # Counted before judging, so the voicemail calls about to be skipped
        # still teach the fingerprints. See `announcements`.
        verdict = await announcements.judge(
            session,
            brand_id,
            segments,
            repeats=config.announcement_repeats,
            min_speech_seconds=config.min_speech_seconds,
        )
        transcript.engine_detail["conversation_seconds"] = verdict.conversation_seconds
        reason = skip_reason(transcript, config) or verdict.reason
        if reason is None:
            await store_transcript(session, recording, brand_id, transcript, config)
        else:
            await _record_failure(session, recording, brand_id, reason, config)
        await _release(session, token)
    return "stored" if reason is None else "skipped"


async def fail(agent: Agent, job_id: str, *, reason: str, retry: bool) -> None:
    """Give a job back, or record it as one that cannot be transcribed.

    `retry` is for the agent's own trouble -- out of GPU memory, a dropped
    download -- which says nothing about the recording. Without it the reason
    is recorded against the recording and it is not handed out again.
    """
    brand_id, token = _parse_job(job_id)
    async with brand_session(brand_id) as session:
        recording_id = await _held_lease(session, agent, token)
        started_at = await _release(session, token)
        if retry:
            if started_at is not None:
                await _move_cursor(session, brand_id, started_at, backwards=True)
            return
        recording = (
            await session.execute(
                select(Recording).where(
                    Recording.brand_id == brand_id, Recording.id == recording_id
                )
            )
        ).scalar_one()
        config = await load_settings(session, brand_id=brand_id)
        await _record_failure(
            session, recording, brand_id, f"agent {agent.name}: {reason}"[:400], config
        )


async def progress(
    *, fresh: bool = False, brand_ids: list[int] | None = None
) -> list[dict]:
    """Per organisation: how much is done, how fast, and when it will finish.

    The rate counts every recording *dealt with* -- transcribed or skipped --
    because that is what empties the backlog, and skipping hold music is most
    of the work. `brand_ids` limits it to the organisations a caller may see;
    None means all of them, for the agent and the CLI. Cached briefly: the
    agent asks on every claim and the console page every few seconds.
    """
    global _progress_cache
    now = time.monotonic()
    if not fresh and _progress_cache and now - _progress_cache[0] < _PROGRESS_CACHE_SECONDS:
        rows = _progress_cache[1]
    else:
        rows = await _measure_progress()
        _progress_cache = (now, rows)
    if brand_ids is None:
        return rows
    return [row for row in rows if row["brand_id"] in brand_ids]


async def _measure_progress() -> list[dict]:
    from c2w.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        brands = (
            await session.execute(
                select(Brand.id, Brand.name).where(Brand.is_active.is_(True)).order_by(Brand.id)
            )
        ).all()

    out = []
    for brand_id, name in brands:
        async with brand_session(brand_id) as session:
            archived = (
                await session.execute(
                    text(
                        "SELECT coalesce(sum(objects_stored), 0) FROM storage_destinations "
                        "WHERE brand_id = :b"
                    ),
                    {"b": brand_id},
                )
            ).scalar_one()
            processed = (
                await session.execute(
                    text("SELECT count(*) FROM transcripts WHERE brand_id = :b"), {"b": brand_id}
                )
            ).scalar_one()
            # The windows read the (brand_id, updated_at) index, so they cost
            # the last day's rows rather than the whole table.
            recent = (
                await session.execute(
                    text(
                        "SELECT "
                        "  count(*) FILTER (WHERE updated_at > now() - interval '1 hour'), "
                        "  count(*), "
                        "  count(*) FILTER (WHERE text IS NOT NULL) "
                        "FROM transcripts "
                        "WHERE brand_id = :b AND updated_at > now() - interval '1 day'"
                    ),
                    {"b": brand_id},
                )
            ).one()
        last_hour, last_day, speech_day = (int(v) for v in recent)
        archived, processed = int(archived), int(processed)
        remaining = max(archived - processed, 0)
        per_hour = last_hour or last_day / 24
        out.append(
            {
                "brand_id": brand_id,
                "organisation": name,
                "archived": archived,
                "processed": processed,
                "remaining": remaining,
                "pct": round(100 * processed / archived, 2) if archived else 0.0,
                "per_hour": round(per_hour, 1),
                "last_24h": last_day,
                "with_speech_last_24h": speech_day,
                "eta_hours": round(remaining / per_hour, 1) if per_hour else None,
            }
        )
    return out


async def _held_lease(session: AsyncSession, agent: Agent, token: str) -> int:
    row = (
        await session.execute(
            text(
                "SELECT recording_id, agent_id FROM transcribe_leases "
                "WHERE token = :t AND expires_at > now()"
            ),
            {"t": token},
        )
    ).first()
    if row is None or int(row.agent_id) != agent.id:
        raise LeaseGone("this job is no longer held by this agent")
    return int(row.recording_id)


async def _release(session: AsyncSession, token: str) -> datetime | None:
    return (
        await session.execute(
            text("DELETE FROM transcribe_leases WHERE token = :t RETURNING started_at"),
            {"t": token},
        )
    ).scalar()


async def _move_cursor(
    session: AsyncSession, brand_id: int, to: datetime, *, backwards: bool = False
) -> None:
    """Advance the cursor to `to`, or with `backwards` pull it back to it."""
    pick = "LEAST" if backwards else "GREATEST"
    await session.execute(
        text(  # `pick` is one of two literals above
            "INSERT INTO transcribe_cursors (brand_id, started_from) VALUES (:b, :t) "  # noqa: S608
            "ON CONFLICT (brand_id) DO UPDATE SET "
            f"started_from = {pick}(transcribe_cursors.started_from, EXCLUDED.started_from), "
            "updated_at = now()"
        ),
        {"b": brand_id, "t": to},
    )


async def _presign(
    session: AsyncSession, objects: list[tuple[StorageDestination, str]]
) -> list[str]:
    from c2w.storage.factory import open_destination

    stores: dict[int, object] = {}
    urls = []
    try:
        for destination, key in objects:
            store = stores.get(destination.id)
            if store is None:
                store = await open_destination(session, destination)
                await store.__aenter__()
                stores[destination.id] = store
            urls.append(await store.presign_get(key, ttl_seconds=LEASE_SECONDS))
    finally:
        for store in stores.values():
            await store.__aexit__(None, None, None)
    return urls


def _parse_job(job_id: str) -> tuple[int, str]:
    brand, _, token = job_id.partition(".")
    if not brand.isdigit() or not token:
        raise LeaseGone("not a job id")
    return int(brand), token


def _suffix(key: str) -> str:
    dot = key.rfind(".")
    return key[dot:] if dot > key.rfind("/") else ".flac"


def _bounded(value) -> float | None:
    try:
        return round(min(1.0, max(0.0, float(value))), 4)
    except (TypeError, ValueError):
        return None


def _json(value: dict) -> str:
    import json

    return json.dumps(value, default=str)
