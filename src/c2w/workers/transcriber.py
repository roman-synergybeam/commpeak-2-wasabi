"""Transcriber: archived recordings into searchable text, in its own process.

This ran inside the scheduler tick until it was moved here, and the move is
the point of the module rather than an incidental tidy-up.

The scheduler executes its steps in sequence, and one of them -- the
incremental inventory -- walks a day prefix at a time across every CommPeak
account and has run for hours on this estate. Transcription sat after it, so
"at most once a minute" actually meant "whenever the scan happens to finish",
and then a recogniser that takes minutes per recording held the *next* tick
behind itself in turn. Two slow things sharing one loop, each making the other
late.

Nothing required them to share it. Transcription reads finished objects out of
the **archive**; the transfer path reads CommPeak and writes the archive. They
overlap on no connection, no bucket and no queue, so this process runs flat out
alongside a transfer that will take weeks, and neither waits for the other.

Runs as a singleton for the same reason the scheduler does: two of these would
transcribe the same recordings twice, which is wasted CPU rather than wrong
data, but on a machine with no GPU wasted CPU is the whole budget.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from sqlalchemy import text

from c2w.db.session import dispose_engine, platform_session
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.transcribe.base import EngineUnavailable
from c2w.transcribe.service import (
    TranscribeSettings,
    build_engine,
    load_settings,
    transcribe_recording,
)
from c2w.workers.scheduler import singleton_lock

log = get_logger(__name__)

#: How long to wait before looking again when there was nothing to do. Short,
#: because the cost of an empty pass is one indexed query per organisation and
#: the backlog is fed by transfers completing at unpredictable times.
IDLE_SECONDS = 30.0

#: How long to wait after a pass that *did* work. Not zero: a tight loop with
#: no pause makes a failing engine spin, and the next batch is claimed
#: immediately anyway because the pass only returns when its batch is done.
BUSY_SECONDS = 1.0

#: Waited after an engine refuses to build, before that organisation is tried
#: again. A misconfigured engine is a person's problem, so complaining once a
#: minute in the log is enough and complaining every thirty seconds is noise.
UNAVAILABLE_SECONDS = 300.0


class TranscriberService:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        #: Loop time after which each organisation may be tried again. Used
        #: only to back off an engine that cannot build.
        self._retry_after: dict[int, float] = {}

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        log.info("transcriber.started")
        while not self._stopping.is_set():
            worked = 0
            try:
                worked = await self._pass()
            except Exception as exc:
                log.exception("transcriber.pass_failed", error=str(exc))
            delay = BUSY_SECONDS if worked else IDLE_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
        log.info("transcriber.stopped")

    async def _pass(self) -> int:
        """One sweep across every organisation. Returns how many were done."""
        async with platform_session() as session:
            brands = (
                await session.execute(text("SELECT id, name FROM brands WHERE is_active"))
            ).all()

        done = 0
        for brand_id, brand_name in brands:
            if self._stopping.is_set():
                break
            done += await self._pass_for_brand(brand_id, brand_name)
        return done

    async def _pass_for_brand(self, brand_id: int, brand_name: str) -> int:
        now = asyncio.get_running_loop().time()
        if now < self._retry_after.get(brand_id, 0.0):
            return 0

        async with platform_session() as session:
            config = await load_settings(session, brand_id=brand_id)
        if not config.enabled:
            return 0

        try:
            engine = build_engine(config)
        except EngineUnavailable as exc:
            # A recogniser nobody built is a configuration problem for a
            # person, so say it and stand down for a while rather than
            # repeating the same refusal every thirty seconds for ever.
            self._retry_after[brand_id] = now + UNAVAILABLE_SECONDS
            log.warning(
                "transcribe.engine_unavailable",
                brand=brand_name,
                brand_id=brand_id,
                engine=config.engine,
                detail=str(exc),
                retry_in_seconds=int(UNAVAILABLE_SECONDS),
            )
            return 0

        pending = await _pending_recordings(brand_id, limit=max(1, config.concurrency))
        if not pending:
            return 0

        # Each task takes its own session: a session is not safe to use from
        # two tasks at once, and the whole point of the concurrency setting is
        # that these overlap.
        results = await asyncio.gather(
            *(
                self._transcribe_one(brand_id, brand_name, rec_id, engine, config)
                for rec_id in pending
            ),
            return_exceptions=True,
        )
        for outcome in results:
            if isinstance(outcome, BaseException):
                # `log.error`, not `log.exception`: there is no exception being
                # handled here -- `gather` returned it as a value -- so
                # `exc_info` would record "NoneType: None" and look like a
                # logging bug rather than a transcription one. The type is in
                # the message because the string alone is often empty.
                log.error(
                    "transcribe.task_failed",
                    brand_id=brand_id,
                    error=f"{type(outcome).__name__}: {outcome}"[:200],
                )
        return sum(1 for outcome in results if outcome is True)

    async def _transcribe_one(
        self,
        brand_id: int,
        brand_name: str,
        recording_id: int,
        engine,
        config: TranscribeSettings,
    ) -> bool:
        async with platform_session() as session:
            await _scope(session, brand_id)
            try:
                result = await transcribe_recording(
                    session,
                    recording_id,
                    brand_id=brand_id,
                    engine=engine,
                    config=config,
                )
                await session.commit()
            except EngineUnavailable as exc:
                await session.rollback()
                self._retry_after[brand_id] = (
                    asyncio.get_running_loop().time() + UNAVAILABLE_SECONDS
                )
                log.warning(
                    "transcribe.engine_unavailable",
                    brand=brand_name,
                    brand_id=brand_id,
                    detail=str(exc),
                )
                return False
            except Exception as exc:
                await session.rollback()
                log.exception(
                    "transcribe.recording_failed",
                    brand_id=brand_id,
                    recording_id=recording_id,
                    error=str(exc)[:200],
                )
                return False

        if result is None:
            # An ordinary "nothing to do" -- too short, not archived yet,
            # already transcribed. `transcribe_recording` has recorded why, so
            # it will not be picked up again and this is not worth a line.
            return False

        log.info(
            "transcribe.done",
            brand=brand_name,
            recording_id=recording_id,
            seconds=result.duration_seconds,
            segments=len(result.segments),
            language=result.language_detected,
        )
        return True


async def _scope(session, brand_id: int) -> None:
    """Set the row-level-security scope for this transaction.

    `set_config(..., true)` is transaction-local and ends at commit, so every
    transaction that touches brand data sets it again.

    **This does not isolate anything here, and must not be relied on to.**
    These sessions are `platform_session()`, which connects as `c2w_platform`
    -- and that role has `BYPASSRLS`, because the workers legitimately read
    across every organisation. A role with `BYPASSRLS` never consults a policy,
    so on this connection the setting above is decorative: the query returns
    every organisation's rows whatever it is set to.

    It is still set, because the `WITH CHECK` on an insert is what writes the
    audit and transcript rows under the right organisation, and because a
    session that later hands off to a non-bypassing role must not inherit an
    empty scope. But every *read* in this module filters on `brand_id`
    explicitly. That is not belt-and-braces; it is the only thing separating
    the organisations here.
    """
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
    )


async def _pending_recordings(brand_id: int, *, limit: int) -> list[int]:
    """Archived recordings for this organisation with no transcript row yet.

    Only archived ones: the audio is read from the archive, never from
    CommPeak. Pulling 13.9 TB through here a second time to transcribe it
    would double the transfer this system exists to do once.

    A recording that failed, or was skipped for being too short, has a
    `transcripts` row and is therefore not returned -- that is what stops a
    permanently unreadable file being retried for ever at the head of the
    queue. Newest first, because a transcript is most wanted for a call
    somebody might still act on.

    `r.brand_id = :brand` is load-bearing and is why this query is not simply
    left to row-level security. The connection is the `BYPASSRLS` platform
    role, so without that clause every organisation's pass sees every other
    organisation's recordings: the first pass to run claims the newest
    recordings on the estate, transcribes them under *its own* settings, and
    writes the transcripts into *its own* partition. That is what happened --
    Go4Rex's pass transcribed InterMagnum's calls -- and it is not a
    performance detail but a leak of one customer's conversations into
    another's records. See `_scope`.
    """
    async with platform_session() as session:
        await _scope(session, brand_id)
        rows = (
            await session.execute(
                text(
                    "SELECT r.id FROM recordings r "
                    "LEFT JOIN transcripts t ON t.recording_id = r.id "
                    "  AND t.brand_id = r.brand_id "
                    "WHERE r.brand_id = :brand "
                    "  AND r.state IN ('AVAILABLE', 'SOURCE_DELETED') "
                    # Both, not just the key: `transcribe_recording` needs the
                    # destination to know which archive to open, and returns
                    # "nothing to do" without writing a row if it is missing --
                    # so a row with one and not the other would be handed back
                    # by this query on every pass, for ever.
                    "  AND r.destination_key IS NOT NULL "
                    "  AND r.destination_id IS NOT NULL "
                    "  AND t.id IS NULL "
                    "ORDER BY r.started_at DESC NULLS LAST "
                    "LIMIT :n"
                ),
                {"n": limit, "brand": brand_id},
            )
        ).scalars().all()
    return [int(r) for r in rows]


async def _amain() -> None:
    configure_logging("c2w-transcriber")
    async with platform_session() as session:
        await reconfigure_from_settings("c2w-transcriber", session)

    async with singleton_lock("c2w-transcriber") as acquired:
        if not acquired:
            log.error(
                "transcriber.already_running",
                detail="another transcriber holds the lock; this instance is exiting",
            )
            return

        service = TranscriberService()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, service.request_stop)
        try:
            await service.run()
        finally:
            await dispose_engine()


def main() -> int:
    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
