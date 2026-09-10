"""Scheduler: incremental inventory, CDR polling and retention gating.

Runs as a singleton.  Two schedulers would double-queue every scan, and since
each scan can enqueue millions of jobs, that is not a harmless duplicate.

Each activity is independent and failure-isolated: a CommPeak connection whose
ACL has lapsed must not stop the others from being scanned.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from c2w.alerts.base import Alert, Severity, dispatch
from c2w.db.base import ConnectionStatus, JobKind, RecordingState, SyncRunKind
from c2w.db.models.core import Brand, CommPeakConnection, Recording, SyncRun
from c2w.db.session import dispose_engine, get_platform_engine, platform_session
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.settings import settings_service
from c2w.storage.errors import TransferError, classify_exception
from c2w.storage.factory import open_source
from c2w.sync import queue
from c2w.sync.inventory import backfill_priority, plan_incremental, scan_range

log = get_logger(__name__)

TICK_SECONDS = 30.0


class Scheduler:
    def __init__(self) -> None:
        self._stopping = asyncio.Event()
        self._last_inventory = 0.0
        self._last_watch = 0.0
        self._last_summary = 0.0
        self._last_cdr = 0.0
        self._last_retention = 0.0
        self._last_sms = 0.0
        self._last_transcribe = 0.0

    def request_stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        log.info("scheduler.started")
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.exception("scheduler.tick_failed", error=str(exc))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
        log.info("scheduler.stopped")

    async def _tick(self) -> None:
        loop_now = asyncio.get_running_loop().time()
        async with platform_session() as session:
            poll = await settings_service.get_int(session, "source.incremental_poll_seconds")

        if loop_now - self._last_inventory >= poll:
            self._last_inventory = loop_now
            await self._run_incremental_inventory()

        # Call records. Its own interval and its own pass: the CDR API is a
        # different service from the recordings bucket, reachable when that one
        # is not, and a call's row is finalised when the call ends rather than
        # when it starts -- so this re-reads an overlapping window.
        async with platform_session() as session:
            cdr_minutes = await settings_service.get_int(session, "commpeak.cdr_poll_minutes")
        if cdr_minutes and loop_now - self._last_cdr >= cdr_minutes * 60:
            self._last_cdr = loop_now
            await _poll_call_records()

        # Accounts that are not working get re-checked here and nowhere else,
        # one at a time. See _watch_access.
        async with platform_session() as session:
            watch_every = await settings_service.get_int(
                session, "source.watch_interval_seconds"
            )
        if loop_now - self._last_watch >= max(60, watch_every):
            self._last_watch = loop_now
            await self._watch_access()

        # Text messages, for organisations that have switched them on. Its own
        # interval because it is a different API with a different rhythm: a
        # delivery receipt can arrive hours after the message, so this re-reads
        # a wide window rather than only asking for what is new.
        async with platform_session() as session:
            sms_minutes = await settings_service.get_int(session, "sms.poll_minutes")
        if loop_now - self._last_sms >= max(60, sms_minutes * 60):
            self._last_sms = loop_now
            await _poll_text_messages()

        # Transcription, for organisations that have switched it on. Every
        # minute at most: each pass takes only as many recordings as the
        # concurrency setting allows, so a long backlog drains steadily
        # instead of one pass running for hours.
        if loop_now - self._last_transcribe >= 60:
            self._last_transcribe = loop_now
            await _transcribe_pending()

        # What the last period actually moved, per organisation. Its own
        # interval because it is a report rather than work.
        async with platform_session() as session:
            summary_minutes = await settings_service.get_int(
                session, "alerts.sync_summary_minutes"
            )
        if summary_minutes and loop_now - self._last_summary >= summary_minutes * 60:
            first_pass = self._last_summary == 0.0
            self._last_summary = loop_now
            # Not on the very first tick: the window would start at process
            # start and report a few seconds of activity as if it were an hour.
            if not first_pass:
                await self._send_sync_summary(summary_minutes)

        # Retention only gates which discovered recordings become eligible for
        # offload; it never deletes anything at the source.
        if loop_now - self._last_retention >= 3600:
            self._last_retention = loop_now
            await self._queue_eligible_recordings()

    async def _run_incremental_inventory(self) -> None:
        """Scan every account that is currently working.

        An account in ERROR is deliberately skipped and left to
        :meth:`_watch_access`. It used to be scanned like the rest, which meant
        eight refused accounts produced a failed request each every five
        minutes -- around ninety-six an hour, for ever. Against a source that
        rate-limits, and whose rate-limited refusal is indistinguishable from a
        blocked address, that is not a retry policy: it is what keeps the block
        alive. The watch retries these, one at a time, and hands an account
        back here the moment it works.
        """
        async with platform_session() as session:
            connections = (
                (
                    await session.execute(
                        select(CommPeakConnection).where(
                            CommPeakConnection.is_enabled.is_(True),
                            CommPeakConnection.status != ConnectionStatus.ERROR,
                        )
                    )
                )
                .scalars()
                .all()
            )
        for connection in connections:
            try:
                await self._scan_connection(connection.id)
            except Exception as exc:
                log.warning(
                    "scheduler.inventory_failed",
                    connection_id=connection.id,
                    error=str(exc),
                )

    async def _send_sync_summary(self, window_minutes: int) -> None:
        """Report what the last window actually moved, per organisation.

        Sent per organisation rather than as one platform-wide message,
        because the two brands are unrelated companies and their figures do not
        belong in the same message -- the same reason every other alert carries
        a brand.

        Sent **even when nothing moved**, unless switched off. A monitoring
        message that only arrives when there is news teaches you nothing from
        silence: an idle system and a stopped one look identical. This is the
        message that distinguishes them, so "nothing moved" is a valid and
        useful thing for it to say.

        Everything here is read from `sync_runs` and `recordings`, so it
        reports what was actually recorded rather than what this process
        happens to remember -- a scheduler restart does not blank the numbers.
        """
        since = datetime.now(UTC) - timedelta(minutes=window_minutes)

        async with platform_session() as session:
            quiet_when_idle = await settings_service.get_bool(
                session, "alerts.sync_summary_quiet_when_idle"
            )
            brands = (
                (await session.execute(select(Brand).order_by(Brand.name))).scalars().all()
            )

            for brand in brands:
                moved = (
                    await session.execute(
                        text(
                            """
                            SELECT coalesce(sum(discovered), 0) AS discovered,
                                   coalesce(sum(queued), 0)     AS queued,
                                   coalesce(sum(transferred), 0) AS transferred,
                                   coalesce(sum(failed), 0)      AS failed,
                                   coalesce(sum(bytes_transferred), 0) AS bytes,
                                   count(*)                      AS runs,
                                   count(*) FILTER (WHERE ok IS FALSE) AS bad_runs
                            FROM sync_runs
                            WHERE brand_id = :b AND started_at >= :since
                            """
                        ),
                        {"b": brand.id, "since": since},
                    )
                ).mappings().one()

                # What was actually copied in the window, read from the
                # recordings themselves rather than from `sync_runs`.
                #
                # `sync_runs.transferred` is written by inventory, not by the
                # worker, so it reported "Copied: 0" while 485 recordings sat
                # verified in the archive -- the single number this message
                # exists to carry, and it was wrong. `verified_at` is the
                # honest source: it is set when a copy has been checked byte
                # for byte, which is the only point at which "copied" is true.
                copied = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) AS n,
                                   coalesce(sum(destination_size), 0) AS bytes
                            FROM recordings
                            WHERE brand_id = :b AND verified_at >= :since
                            """
                        ),
                        {"b": brand.id, "since": since},
                    )
                ).mappings().one()

                # The standing position, not just the window: "54,525 waiting"
                # is the number somebody wants when deciding whether to worry.
                # QUEUED and DISCOVERED are both waiting -- queued has a job,
                # discovered does not yet -- and collapsing them into one
                # "waiting" figure reported 0 while 54,525 were outstanding.
                state = (
                    await session.execute(
                        text(
                            """
                            SELECT count(*) FILTER (
                                       WHERE state IN ('DISCOVERED', 'QUEUED')
                                   ) AS waiting,
                                   count(*) FILTER (WHERE state = 'AVAILABLE')  AS archived,
                                   count(*) FILTER (WHERE state = 'FAILED')     AS failed,
                                   count(*)                                     AS total
                            FROM recordings WHERE brand_id = :b
                            """
                        ),
                        {"b": brand.id},
                    )
                ).mappings().one()

                if quiet_when_idle and not (
                    int(moved["discovered"] or 0)
                    or int(copied["n"] or 0)
                    or int(state["failed"] or 0)
                ):
                    continue

                accounts = (
                    await session.execute(
                        text(
                            """
                            SELECT c.name,
                                   c.status::text AS status,
                                   to_char(c.inventory_cursor_day, 'YYYY-MM-DD') AS scanned_to
                            FROM commpeak_connections c
                            WHERE c.brand_id = :b AND c.is_enabled
                            ORDER BY c.name
                            """
                        ),
                        {"b": brand.id},
                    )
                ).mappings().all()

                # A tick per account, so the shape of the message tells you
                # whether anything is wrong before you read any of it. The four
                # pill meanings of the console UI, in the one form a chat has.
                marks = {"OK": "\u2705", "ERROR": "\u274c", "DISABLED": "\u23f8"}
                lines = [
                    f"{row['name']} \u2014 {marks.get(row['status'], '\u2753')} "
                    + (
                        f"scanned to {row['scanned_to']}"
                        if row["scanned_to"]
                        else "not scanned yet"
                    )
                    for row in accounts
                ]
                per_account = "*CommPeak*\n" + (
                    "\n".join(lines) or "no accounts configured"
                )

                # The archive side. "Copied 536" answers what moved; it does
                # not answer what is actually *in* the archive, which is the
                # question somebody asks when deciding whether the migration
                # is working. Read per destination, because a brand may have
                # more than one bucket.
                archives = (
                    await session.execute(
                        text(
                            """
                            SELECT d.name, d.bucket, d.region, d.status::text AS status,
                                   count(r.id)                       AS objects,
                                   coalesce(sum(r.destination_size), 0) AS bytes
                            FROM storage_destinations d
                            LEFT JOIN recordings r
                                   ON r.destination_id = d.id
                                  AND r.brand_id = d.brand_id
                                  AND r.verified_at IS NOT NULL
                            WHERE d.brand_id = :b
                            GROUP BY d.name, d.bucket, d.region, d.status
                            ORDER BY d.name
                            """
                        ),
                        {"b": brand.id},
                    )
                ).mappings().all()

                if archives:
                    archive_lines = [
                        f"{a['bucket']} ({a['region']}) "
                        f"{marks.get(a['status'], '\u2753')} "
                        f"{int(a['objects'] or 0):,} objects, "
                        f"{int(a['bytes'] or 0) / 1e9:.2f} GB"
                        for a in archives
                    ]
                    per_account += "\n\n*Wasabi*\n" + "\n".join(archive_lines)
                else:
                    per_account += "\n\n*Wasabi*\nno archive bucket configured"

                # `sum()` over a bigint column comes back as Decimal, which
                # will not divide by a float.
                gb = int(copied["bytes"] or 0) / 1e9
                bad = int(moved["bad_runs"] or 0)
                severity = (
                    Severity.WARNING
                    if (bad or int(state["failed"] or 0))
                    else Severity.INFO
                )

                await dispatch(
                    session,
                    Alert(
                        title=f"Sync summary, last {window_minutes} min",
                        body=per_account,
                        severity=severity,
                        brand_id=brand.id,
                        brand_name=brand.name,
                        # Per brand and per window, so a summary is never
                        # suppressed as a duplicate of the previous one.
                        dedupe_key=f"sync-summary:{brand.id}:{since:%Y%m%d%H%M}",
                        fields={
                            "Found": f"{int(moved['discovered'] or 0):,}",
                            "Queued": f"{int(moved['queued'] or 0):,}",
                            "Copied": f"{int(copied['n'] or 0):,} ({gb:.2f} GB)",
                            # Standing failures, matching what sets the
                            # severity above. Reporting the window's count here
                            # while colouring the message from the standing one
                            # would let an amber alert say "Failed: 0".
                            "Failed": f"{int(state['failed'] or 0):,}",
                            "Scans": f"{int(moved['runs'] or 0):,}"
                            + (f", {bad} failed" if bad else ""),
                            "Waiting to copy": f"{state['waiting']:,}",
                            "In the archive": f"{state['archived']:,} of {state['total']:,}",
                        },
                    ),
                )
                log.info(
                    "scheduler.sync_summary_sent",
                    brand_id=brand.id,
                    discovered=int(moved["discovered"]),
                    copied=int(copied["n"] or 0),
                )

    async def _watch_access(self) -> None:
        """Re-check one failing account, and say so when one starts working.

        Written because the alternative was a person sitting on the settings
        page pressing Test -- which is how the source came to refuse every
        account in the first place, roughly twenty checks in half an hour being
        enough to trip its rate limit.

        Three properties matter, and each is a decision:

        * **One account per turn, oldest check first.** Eight accounts spread
          over eight turns rather than eight requests at once. Never a burst,
          by construction rather than by hoping the interval is generous.
        * **A single cheap listing**, not the full onboarding self-test, which
          also downloads a sample object. The question here is only "does this
          answer at all".
        * **The alert is on the transition**, not on the state. Nobody needs
          telling every two minutes that an account is still refused; the one
          worth interrupting somebody for is the change.
        """
        async with platform_session() as session:
            if not await settings_service.get_bool(session, "source.watch_enabled"):
                return
            # Oldest check first, NULLs first, so a never-checked account goes
            # to the front and no account can be starved.
            connection = (
                await session.execute(
                    select(CommPeakConnection)
                    .where(
                        CommPeakConnection.is_enabled.is_(True),
                        CommPeakConnection.status == ConnectionStatus.ERROR,
                    )
                    .order_by(CommPeakConnection.last_probe_at.asc().nullsfirst())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if connection is None:
                return

            name = connection.name
            brand_id = connection.brand_id
            was = connection.status_detail or ""
            try:
                source = await open_source(session, connection)
                async with source as src:
                    # One delimited listing: a single request, and the reply is
                    # just the top-level year prefixes however large the bucket
                    # is. Enough to prove the address is accepted and the
                    # signature is valid, which is the whole question.
                    await src.list_common_prefixes("")
            except Exception as exc:
                error = exc if isinstance(exc, TransferError) else classify_exception(exc)
                connection.status_detail = str(error)[:4000]
                connection.last_probe_at = datetime.now(UTC)
                await session.commit()
                # Debug, not warning: this is the expected answer while an
                # account is down, and at one line per turn it would otherwise
                # be the only thing in the log.
                log.debug(
                    "scheduler.watch_still_failing",
                    connection_id=connection.id,
                    error=str(error)[:200],
                )
                return

            connection.status = ConnectionStatus.OK
            connection.status_detail = None
            connection.last_probe_at = datetime.now(UTC)
            brand = (
                await session.execute(select(Brand).where(Brand.id == brand_id))
            ).scalar_one_or_none()
            await session.commit()

        log.info("scheduler.watch_recovered", connection_id=connection.id, account=name)
        async with platform_session() as session:
            await dispatch(
                session,
                Alert(
                    title="CommPeak account is working again",
                    body=(
                        f"{name} answered a bucket listing, after previously "
                        f"failing with: {was[:200] or 'an unknown error'}\n\n"
                        "Inventory has resumed for it; nothing needs doing."
                    ),
                    severity=Severity.INFO,
                    brand_id=brand_id,
                    brand_name=brand.name if brand else None,
                    # Per account, so eight recoveries are eight messages
                    # rather than one and seven suppressed as duplicates.
                    dedupe_key=f"commpeak-recovered:{connection.id}",
                    fields={"Account": name, "Bucket": connection.s3_bucket},
                ),
            )

    async def _scan_connection(self, connection_id: int) -> None:
        async with platform_session() as session:
            connection = (
                await session.execute(
                    select(CommPeakConnection).where(CommPeakConnection.id == connection_id)
                )
            ).scalar_one()
            overlap = await settings_service.get_int(session, "source.incremental_overlap_hours")
            page_size = await settings_service.get_int(session, "source.list_page_size")
            transfers_on = await settings_service.get_bool(session, "transfer.enabled")

            start, end = plan_incremental(connection, overlap_hours=overlap)
            run = SyncRun(
                brand_id=connection.brand_id,
                connection_id=connection.id,
                kind=SyncRunKind.INCREMENTAL,
            )
            session.add(run)
            await session.flush()

            try:
                source = await open_source(session, connection)
                async with source as src:
                    result = await scan_range(
                        session,
                        src,
                        connection,
                        start=start,
                        end=end,
                        page_size=page_size,
                        destination_id=connection.destination_id if transfers_on else None,
                        enqueue_transfers=transfers_on,
                        commit_every_prefix=False,
                    )
            except Exception as exc:
                error = exc if isinstance(exc, TransferError) else classify_exception(exc)
                run.finished_at = datetime.now(UTC)
                run.ok = False
                run.error_detail = str(error)[:4000]
                connection.status = ConnectionStatus.ERROR
                connection.status_detail = str(error)[:4000]
                connection.last_probe_at = datetime.now(UTC)
                raise

            run.finished_at = datetime.now(UTC)
            run.ok = True
            run.discovered = result.recordings_new
            run.queued = result.queued
            run.detail = {
                "prefixes": result.prefixes_scanned,
                "objects_seen": result.objects_seen,
                "orphans": result.orphans,
                "match_methods": result.match_methods,
                "bytes_seen": result.bytes_seen,
            }
            connection.status = ConnectionStatus.OK
            connection.status_detail = None
            connection.last_probe_at = datetime.now(UTC)

            if result.recordings_new:
                log.info(
                    "scheduler.inventory_complete",
                    connection_id=connection.id,
                    new=result.recordings_new,
                    queued=result.queued,
                    orphans=result.orphans,
                )

    async def _queue_eligible_recordings(self) -> None:
        """Queue recordings that have aged past their brand's offload window.

        Discovery and offload are separate steps: a recording is inventoried
        immediately so it is searchable, but only copied once it is older than
        the retention window -- there is no point archiving a call that CommPeak
        is still holding anyway. This is also the path that picks up everything
        discovered while no destination existed.
        """
        async with platform_session() as session:
            if not await settings_service.get_bool(session, "transfer.enabled"):
                return

            connections = (
                (
                    await session.execute(
                        select(CommPeakConnection).where(
                            CommPeakConnection.is_enabled.is_(True),
                            CommPeakConnection.destination_id.isnot(None),
                        )
                    )
                )
                .scalars()
                .all()
            )

            for connection in connections:
                days = await settings_service.get_int(
                    session, "retention.offload_after_days", brand_id=connection.brand_id
                )
                cutoff = datetime.now(UTC) - timedelta(days=days)
                pending = (
                    (
                        await session.execute(
                            select(Recording)
                            .where(
                                Recording.brand_id == connection.brand_id,
                                Recording.connection_id == connection.id,
                                Recording.state == RecordingState.DISCOVERED,
                                Recording.started_at <= cutoff,
                            )
                            .order_by(Recording.started_at.desc())
                            .limit(5000)
                        )
                    )
                    .scalars()
                    .all()
                )
                for recording in pending:
                    await queue.enqueue(
                        session,
                        brand_id=recording.brand_id,
                        connection_id=connection.id,
                        recording_id=recording.id,
                        destination_id=connection.destination_id,
                        kind=JobKind.TRANSFER,
                        priority=backfill_priority(recording.started_at),
                    )
                    recording.state = RecordingState.QUEUED
                    recording.destination_id = connection.destination_id

                if pending:
                    session.add(
                        SyncRun(
                            brand_id=connection.brand_id,
                            connection_id=connection.id,
                            kind=SyncRunKind.RETENTION,
                            finished_at=datetime.now(UTC),
                            ok=True,
                            queued=len(pending),
                            detail={"offload_after_days": days},
                        )
                    )
                    log.info(
                        "scheduler.queued_eligible",
                        connection_id=connection.id,
                        queued=len(pending),
                        offload_after_days=days,
                    )


async def _transcribe_pending() -> None:
    """Transcribe archived recordings that have no transcript yet.

    Only archived ones: the audio is read from the archive, never from
    CommPeak. Pulling 13.9 TB through here a second time to transcribe it would
    double the transfer this system exists to do once.

    A failure is written to the transcript row rather than retried for ever, so
    one unreadable file cannot block the queue behind it.
    """
    from c2w.transcribe.base import EngineUnavailable
    from c2w.transcribe.service import build_engine, load_settings, transcribe_recording

    async with platform_session() as session:
        brands = (
            await session.execute(text("SELECT id, name FROM brands WHERE is_active"))
        ).all()

    for brand_id, brand_name in brands:
        async with platform_session() as session:
            config = await load_settings(session, brand_id=brand_id)
            if not config.enabled:
                continue
            try:
                engine = build_engine(config)
            except EngineUnavailable as exc:
                # A recogniser nobody built is a configuration problem for a
                # person, so say it once per pass and move on rather than
                # failing every recording individually.
                log.warning(
                    "transcribe.engine_unavailable",
                    brand_id=brand_id,
                    engine=config.engine,
                    detail=str(exc),
                )
                continue

            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
            )
            pending = (
                await session.execute(
                    text(
                        "SELECT r.id FROM recordings r "
                        "LEFT JOIN transcripts t ON t.recording_id = r.id "
                        "  AND t.brand_id = r.brand_id "
                        "WHERE r.state IN ('AVAILABLE', 'SOURCE_DELETED') "
                        "  AND r.destination_key IS NOT NULL "
                        "  AND t.id IS NULL "
                        "ORDER BY r.started_at DESC NULLS LAST "
                        "LIMIT :n"
                    ),
                    {"n": max(1, config.concurrency)},
                )
            ).scalars().all()

        for recording_id in pending:
            async with platform_session() as session:
                await session.execute(
                    text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
                )
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
                    log.warning(
                        "transcribe.engine_unavailable",
                        brand_id=brand_id,
                        detail=str(exc),
                    )
                    break
                except Exception as exc:
                    await session.rollback()
                    log.exception(
                        "transcribe.pass_failed",
                        recording_id=recording_id,
                        error=str(exc)[:200],
                    )
                    continue
            if result is not None:
                log.info(
                    "transcribe.done",
                    brand=brand_name,
                    recording_id=recording_id,
                    seconds=result.duration_seconds,
                    segments=len(result.segments),
                    language=result.language_detected,
                )


async def _poll_call_records() -> None:
    """Fetch call records for every account that has an address and a key.

    **This did not exist.** The module docstring said "CDR polling", the client
    and `ingest_page` were both written and tested, and nothing ever called
    them -- so `cdrs` stayed empty no matter what was configured, and every
    recording stayed an orphan because there was nothing to correlate against.
    A component that is built, documented and unwired is indistinguishable
    from one that is broken.

    Per account rather than per organisation, because PBX Stats issues its key
    per user per instance: Go4Rex has two Cloud PBX instances and they have
    different keys. The cursor lives on the connection for the same reason.

    Failure is per account. One instance with a lapsed key must not stop the
    others, which is the same rule the inventory pass follows.
    """
    from c2w.commpeak.cdr_client import (
        AuthScheme,
        CdrApiConfig,
        CdrClient,
        ingest_page,
        poll_window,
    )

    async with platform_session() as session:
        connections = list(
            (
                await session.execute(
                    select(CommPeakConnection)
                    .where(CommPeakConnection.is_enabled.is_(True))
                    .order_by(CommPeakConnection.name)
                )
            ).scalars().all()
        )

    for conn in connections:
        async with platform_session() as session:
            fresh = (
                await session.execute(
                    select(CommPeakConnection).where(CommPeakConnection.id == conn.id)
                )
            ).scalar_one()
            if not fresh.cdr_api_base:
                continue
            token = await _open_cdr_key(session, fresh)
            if not token:
                log.debug("cdr.skipped", connection_id=fresh.id, reason="no api key")
                continue

            overlap = await settings_service.get_int(
                session, "commpeak.cdr_overlap_minutes", brand_id=fresh.brand_id
            )
            page_size = await settings_service.get_int(
                session, "commpeak.cdr_page_size", brand_id=fresh.brand_id
            )
            mode = await settings_service.get_str(
                session, "commpeak.cdr_auth_scheme", brand_id=fresh.brand_id
            )
            path = await settings_service.get_str(
                session, "commpeak.cdr_api_path", brand_id=fresh.brand_id
            )

            start, end = poll_window(
                fresh.last_cdr_cursor, overlap_minutes=max(overlap, 1)
            )
            config = CdrApiConfig(
                base_url=fresh.cdr_api_base,
                path=path or "/api/cdrs",
                auth=AuthScheme(mode or "header"),
                token=token,
                username=fresh.cdr_api_user or "",
                page_size=page_size or 500,
            )

            inserted = updated = 0
            try:
                async for records in CdrClient(config).fetch_range(start, end):
                    got_in, got_up = await ingest_page(
                        session,
                        brand_id=fresh.brand_id,
                        connection_id=fresh.id,
                        tenant_id=fresh.tenant_id,
                        records=records,
                    )
                    inserted += got_in
                    updated += got_up
            except Exception as exc:
                log.warning(
                    "cdr.poll_failed",
                    connection_id=fresh.id,
                    account=fresh.name,
                    error=str(exc)[:200],
                )
                continue

            # Advanced only after the rows are stored, so a failure mid-run
            # re-reads the window rather than skipping past it.
            fresh.last_cdr_cursor = end
            await session.commit()

        if inserted or updated:
            log.info(
                "cdr.polled",
                connection_id=conn.id,
                account=conn.name,
                inserted=inserted,
                updated=updated,
            )


async def _open_cdr_key(session, connection) -> str:
    """Unseal one account's CDR API key, or return empty.

    Falls back to the organisation-level `cdr.api_key` setting, so an estate
    where one key covers every instance can be configured once.
    """
    if connection.cdr_api_key_sealed:
        from c2w.storage.factory import open_cdr_api_key

        try:
            return await open_cdr_api_key(session, connection)
        except Exception as exc:
            # A key sealed under a master key that no longer matches is a
            # configuration problem for a person, not something to retry
            # silently every poll -- but it must not stop the other accounts.
            log.warning(
                "cdr.key_unavailable", connection_id=connection.id, error=str(exc)[:120]
            )
            return ""
    return await settings_service.get_secret(
        session, "commpeak.cdr_api_token", brand_id=connection.brand_id
    )


async def _poll_text_messages() -> None:
    """Fetch sent and received messages for every organisation that wants them.

    Per organisation, because the API key, the stream and the switch are all
    per-organisation settings -- and because one company's messages must never
    be written under another's brand id.

    The cursor is stored on the organisation's first CommPeak connection rather
    than on the settings row: TextPeak is billed per account and an
    organisation with no PBX connection at all has no messages to fetch, so
    there is always a connection to hang it on when there is anything to do.
    """
    from c2w.commpeak.sms_client import (
        Direction,
        SmsApiConfig,
        SmsQueryFilters,
        fetch_messages,
        normalise_message,
        poll_window,
        store_messages,
    )

    async with platform_session() as session:
        brands = (
            await session.execute(text("SELECT id, name FROM brands WHERE is_active"))
        ).all()

    for brand_id, brand_name in brands:
        async with platform_session() as session:
            if not await settings_service.get_bool(
                session, "sms.enabled", brand_id=brand_id
            ):
                continue
            token = await settings_service.get_secret(
                session, "sms.api_token", brand_id=brand_id
            )
            if not token:
                log.info("sms.skipped", brand_id=brand_id, reason="no api key")
                continue
            config = SmsApiConfig(
                token=token,
                base_url=await settings_service.get_str(
                    session, "sms.api_base", brand_id=brand_id
                ),
                outgoing_path=await settings_service.get_str(
                    session, "sms.api_path", brand_id=brand_id
                ),
                incoming_path=await settings_service.get_str(
                    session, "sms.incoming_path", brand_id=brand_id
                ),
                stream_id=await settings_service.get_str(
                    session, "sms.stream_id", brand_id=brand_id
                ),
                page_size=await settings_service.get_int(
                    session, "sms.page_size", brand_id=brand_id
                ),
            )
            overlap = await settings_service.get_int(
                session, "sms.overlap_hours", brand_id=brand_id
            )
            row = (
                await session.execute(
                    text(
                        "SELECT id, last_sms_cursor FROM commpeak_connections "
                        "WHERE brand_id = :b ORDER BY id LIMIT 1"
                    ),
                    {"b": brand_id},
                )
            ).first()

        if row is None:
            log.info("sms.skipped", brand_id=brand_id, reason="no commpeak account")
            continue
        connection_id, cursor = row
        start, end = poll_window(cursor, overlap_hours=overlap)
        filters = SmsQueryFilters(start=start, end=end)

        rows: list[dict] = []
        try:
            for direction in (Direction.OUT, Direction.IN):
                items = await fetch_messages(config, direction, filters)
                rows.extend(normalise_message(item, direction) for item in items)
        except Exception as exc:
            # A refused key or an unreachable host must not stop the other
            # organisations, and must not move the cursor -- the window has to
            # be retried, or those messages are lost for good.
            log.warning(
                "sms.poll_failed", brand_id=brand_id, brand=brand_name, error=str(exc)[:200]
            )
            continue

        async with platform_session() as session:
            await session.execute(
                text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
            )
            inserted, updated = await store_messages(
                session, brand_id, rows, connection_id=connection_id
            )
            # Only advanced once the rows are safely stored, so a crash between
            # the fetch and the write re-reads the same window instead of
            # skipping it.
            await session.execute(
                text(
                    "UPDATE commpeak_connections SET last_sms_cursor = :c, "
                    "last_sms_sync_at = now() WHERE id = :i"
                ),
                {"c": end, "i": connection_id},
            )
            await session.commit()
        log.info(
            "sms.polled",
            brand_id=brand_id,
            brand=brand_name,
            fetched=len(rows),
            inserted=inserted,
            updated=updated,
        )


@contextlib.asynccontextmanager
async def singleton_lock(name: str):
    """Hold a PostgreSQL advisory lock for as long as the process runs.

    A second scheduler would double-queue every scan, and a scan can enqueue
    millions of jobs, so this is not a harmless duplicate.

    The lock must be taken on a connection that stays open: advisory locks are
    session-scoped, so acquiring one inside a short-lived session releases it
    the moment that session returns to the pool -- which looks like it worked
    and guards nothing. Holding the connection also means the lock is released
    automatically if the process dies, with no stale-lock cleanup to do.
    """
    engine = get_platform_engine()
    connection = await engine.connect()
    try:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:name))"), {"name": name}
            )
        ).scalar_one()
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": name}
            )
    finally:
        await connection.close()


async def _amain() -> None:
    configure_logging("c2w-scheduler")
    async with platform_session() as session:
        await reconfigure_from_settings("c2w-scheduler", session)

    async with singleton_lock("c2w-scheduler") as acquired:
        if not acquired:
            log.error(
                "scheduler.already_running",
                detail="another scheduler holds the lock; this instance is exiting",
            )
            return

        scheduler = Scheduler()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, scheduler.request_stop)
        try:
            await scheduler.run()
        finally:
            await dispose_engine()


def main() -> int:
    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
