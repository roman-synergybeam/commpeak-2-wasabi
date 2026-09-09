"""End-to-end pipeline: discover -> correlate -> queue -> transfer -> verify.

Runs against a real PostgreSQL (for the queue's SKIP LOCKED semantics and the
partitioned tables) and a real moto S3 server standing in for both CommPeak and
the archive.  Mocking either would hide precisely the behaviour that matters:
concurrent job claims, multipart assembly, and read-back verification.

Skipped unless C2W_TEST_DATABASE_URL is set; see tests/test_brand_isolation.py.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.db.base import JobState, RecordingState
from c2w.db.models.core import CommPeakConnection, Recording, StorageDestination, TransferJob
from c2w.storage.commpeak import CommPeakSource
from c2w.storage.s3_adapter import S3Client
from c2w.storage.wasabi import WasabiDestination
from c2w.sync import queue
from c2w.sync.inventory import backfill_priority, plan_incremental, scan_hour
from c2w.sync.transfer import build_sidecar, transfer_recording, verify_recording

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)

HOUR = datetime(2026, 9, 8, 0, tzinfo=UTC)
# Channel id whose epoch is exactly the call start, as CommPeak names them.
UNIQUEID = int(datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC).timestamp())
SRC_KEY = f"2026/09/08/00/out-593990899917-101-20260908-005217-{UNIQUEID}.0.flac"


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def scenario(db, s3_creds, moto_endpoint):
    """A brand with one CommPeak connection, one destination, and one CDR."""
    brand_id = 1
    src_bucket = f"cp-{uuid.uuid4().hex[:10]}"
    dst_bucket = f"wa-{uuid.uuid4().hex[:10]}"
    for name in (src_bucket, dst_bucket):
        async with S3Client(s3_creds(name)) as c:
            await c.client.create_bucket(Bucket=name)

    async with db() as s:
        await s.execute(
            text(
                "INSERT INTO brands (id, name, slug) VALUES (:i,'Pipeline','pipeline') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"i": brand_id},
        )
        # An explicit id leaves brands_id_seq behind it; repair it so a later
        # sequence-assigned insert cannot collide. See test_brand_isolation.
        await s.execute(
            text("SELECT setval('brands_id_seq', GREATEST(:n, (SELECT max(id) FROM brands)))"),
            {"n": brand_id},
        )
        await s.commit()
        for table in ("cdrs", "recordings"):
            await s.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand_id} "
                    f"PARTITION OF {table} FOR VALUES IN ({brand_id})"
                )
            )
        # These tests share brand 1 and several reuse the same source key on
        # purpose, so each starts from an empty working set.
        await s.execute(text("TRUNCATE transfer_attempts, transfer_jobs, recordings, cdrs CASCADE"))
        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )

        tenant_id = (
            await s.execute(
                text(
                    "INSERT INTO tenants (brand_id, name, slug) "
                    "VALUES (:b, 'go4rex.td', :slug) RETURNING id"
                ),
                {"b": brand_id, "slug": f"td-{uuid.uuid4().hex[:6]}"},
            )
        ).scalar_one()

        dest = StorageDestination(
            brand_id=brand_id,
            name=f"wasabi-{uuid.uuid4().hex[:6]}",
            provider="wasabi",
            endpoint=moto_endpoint,
            region="us-east-1",
            bucket=dst_bucket,
            path_prefix="archive",
            access_key_sealed="x",
            secret_sealed="x",
        )
        s.add(dest)
        await s.flush()

        conn = CommPeakConnection(
            brand_id=brand_id,
            tenant_id=tenant_id,
            name="Go4Rex TD",
            s3_endpoint=moto_endpoint,
            s3_region="us-east-1",
            s3_bucket=src_bucket,
            s3_access_key_sealed="x",
            s3_secret_sealed="x",
            destination_id=dest.id,
        )
        s.add(conn)
        await s.flush()

        # A CDR whose start_at matches the channel id exactly.
        await s.execute(
            text(
                "INSERT INTO cdrs (brand_id, connection_id, tenant_id, call_uuid, start_at, "
                "src, dst, call_duration, src_norm, dst_norm) VALUES "
                "(:b, :c, :t, :u, :start, '593990899917@did.commpeak.com', '0007281', 35, "
                "'990899917', '7281')"
            ),
            {
                "b": brand_id,
                "c": conn.id,
                "t": tenant_id,
                "u": str(uuid.uuid4()),
                "start": datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC),
            },
        )
        await s.commit()
        return {
            "brand_id": brand_id,
            "connection_id": conn.id,
            "destination_id": dest.id,
            "src": s3_creds(src_bucket),
            "dst": s3_creds(dst_bucket),
        }


async def _seed_recording(creds, key: str, data: bytes) -> None:
    async with S3Client(creds) as c:
        await c.client.put_object(Bucket=creds.bucket, Key=key, Body=data)


async def _scoped(db, brand_id: int):
    s = db()
    await s.execute(text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)})
    return s


class TestInventory:
    async def test_discovers_correlates_and_queues(self, db, scenario):
        audio = os.urandom(40_000)
        await _seed_recording(scenario["src"], SRC_KEY, audio)
        # A sidecar-ish artefact that must not become a playable recording.
        await _seed_recording(scenario["src"], "2026/09/08/00/notes.json", b"{}")

        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                result = await scan_hour(
                    s, src, conn, HOUR, destination_id=scenario["destination_id"]
                )
            await s.commit()

        assert result.objects_seen == 2
        assert result.non_audio_skipped == 1, "JSON artefacts must not be treated as media"
        assert result.recordings_new == 1
        assert result.queued == 1
        # The channel id lines up with the CDR exactly, so this is the top tier.
        assert result.match_methods == {"epoch_exact": 1}
        assert result.orphans == 0

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert rec.state is RecordingState.QUEUED
            assert rec.match_method == "epoch_exact"
            assert rec.match_confidence == pytest.approx(0.99)
            assert rec.call_uuid is not None
            assert rec.source_size == len(audio)
            assert rec.uniqueid == UNIQUEID

            job = (
                await s.execute(select(TransferJob).where(TransferJob.recording_id == rec.id))
            ).scalar_one()
            assert job.state is JobState.PENDING

    async def test_rescan_is_idempotent(self, db, scenario):
        """A re-run must not duplicate rows -- at 19M objects, near-idempotent
        would mean millions of duplicate transfers."""
        await _seed_recording(scenario["src"], SRC_KEY, b"audio")
        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                first = await scan_hour(
                    s, src, conn, HOUR, destination_id=scenario["destination_id"]
                )
                second = await scan_hour(
                    s, src, conn, HOUR, destination_id=scenario["destination_id"]
                )
            await s.commit()

        assert first.recordings_new == 1
        assert second.recordings_new == 0
        assert second.recordings_existing == 1

        async with await _scoped(db, scenario["brand_id"]) as s:
            count = (
                await s.execute(
                    select(text("count(*)"))
                    .select_from(Recording)
                    .where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert count == 1

    async def test_inventory_without_destination_still_records(self, db, scenario):
        """While Wasabi buckets are not yet provisioned, discovery and
        correlation must still work; transfers are queued later."""
        await _seed_recording(scenario["src"], SRC_KEY, b"audio")
        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                result = await scan_hour(s, src, conn, HOUR, destination_id=None)
            await s.commit()

        assert result.recordings_new == 1
        assert result.queued == 0, "nothing to queue without an archive destination"
        assert result.correlated == 1, "correlation must not depend on having a destination"

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert rec.state is RecordingState.DISCOVERED


class TestTransfer:
    async def test_full_transfer_verifies_and_writes_sidecar(self, db, scenario):
        audio = os.urandom(30_000)
        await _seed_recording(scenario["src"], SRC_KEY, audio)

        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                await scan_hour(s, src, conn, HOUR, destination_id=scenario["destination_id"])
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            dest_row = (
                await s.execute(
                    select(StorageDestination).where(
                        StorageDestination.id == scenario["destination_id"]
                    )
                )
            ).scalar_one()

            async with (
                CommPeakSource(scenario["src"]) as src,
                WasabiDestination(scenario["dst"]) as dst,
            ):
                outcome = await transfer_recording(
                    s,
                    rec,
                    dest_row,
                    src,
                    dst,
                    brand_slug="go4rex",
                    tenant_slug="go4rex-td",
                    multipart_threshold=16 * 1024 * 1024,
                    multipart_chunk=5 * 1024 * 1024,
                )
            await s.commit()

        assert outcome.verified
        assert outcome.bytes_transferred == len(audio)
        assert outcome.sidecar_written
        # Brand and tenant lead the key so a brand's objects stay contiguous.
        assert outcome.destination_key == f"archive/go4rex/go4rex-td/{SRC_KEY}"

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert rec.state is RecordingState.AVAILABLE
            assert rec.state.playable
            assert rec.verified_at is not None
            assert rec.checksum_sha256

        # The archived bytes must be identical, and the sidecar must describe them.
        async with S3Client(scenario["dst"]) as c:
            got = await c.client.get_object(
                Bucket=scenario["dst"].bucket, Key=outcome.destination_key
            )
            assert await got["Body"].read() == audio

            side = await c.client.get_object(
                Bucket=scenario["dst"].bucket, Key=f"{outcome.destination_key}.cdr.json"
            )
            import json

            payload = json.loads(await side["Body"].read())
            assert payload["schema"] == "c2w.sidecar/1"
            assert payload["recording"]["source_key"] == SRC_KEY
            assert payload["correlation"]["method"] == "epoch_exact"
            assert payload["cdr"]["dst"] == "0007281"

    async def test_source_object_is_untouched(self, db, scenario):
        """The whole platform is read-only against CommPeak."""
        audio = os.urandom(10_000)
        await _seed_recording(scenario["src"], SRC_KEY, audio)

        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                await scan_hour(s, src, conn, HOUR, destination_id=scenario["destination_id"])
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            dest_row = (
                await s.execute(
                    select(StorageDestination).where(
                        StorageDestination.id == scenario["destination_id"]
                    )
                )
            ).scalar_one()
            async with (
                CommPeakSource(scenario["src"]) as src,
                WasabiDestination(scenario["dst"]) as dst,
            ):
                await transfer_recording(
                    s,
                    rec,
                    dest_row,
                    src,
                    dst,
                    brand_slug="go4rex",
                    tenant_slug="go4rex-td",
                    multipart_threshold=16 * 1024 * 1024,
                    multipart_chunk=5 * 1024 * 1024,
                )
            await s.commit()

        async with S3Client(scenario["src"]) as c:
            head = await c.client.head_object(Bucket=scenario["src"].bucket, Key=SRC_KEY)
            assert head["ContentLength"] == len(audio), "source must be unchanged"

    async def test_missing_source_is_not_a_failure(self, db, scenario):
        """An object deleted at source between inventory and transfer is a normal
        condition, not an error to retry forever."""
        await _seed_recording(scenario["src"], SRC_KEY, b"audio")
        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                await scan_hour(s, src, conn, HOUR, destination_id=scenario["destination_id"])
            await s.commit()

        # Remove it from the source, as CommPeak's own retention would.
        async with S3Client(scenario["src"]) as c:
            await c.client.delete_object(Bucket=scenario["src"].bucket, Key=SRC_KEY)

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            dest_row = (
                await s.execute(
                    select(StorageDestination).where(
                        StorageDestination.id == scenario["destination_id"]
                    )
                )
            ).scalar_one()
            async with (
                CommPeakSource(scenario["src"]) as src,
                WasabiDestination(scenario["dst"]) as dst,
            ):
                outcome = await transfer_recording(
                    s,
                    rec,
                    dest_row,
                    src,
                    dst,
                    brand_slug="go4rex",
                    tenant_slug="go4rex-td",
                    multipart_threshold=16 * 1024 * 1024,
                    multipart_chunk=5 * 1024 * 1024,
                )
            await s.commit()

        assert not outcome.verified
        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert rec.state is RecordingState.MISSING_SOURCE

    async def test_verify_requeues_when_archive_object_vanishes(self, db, scenario):
        audio = os.urandom(5_000)
        await _seed_recording(scenario["src"], SRC_KEY, audio)
        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                await scan_hour(s, src, conn, HOUR, destination_id=scenario["destination_id"])
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            dest_row = (
                await s.execute(
                    select(StorageDestination).where(
                        StorageDestination.id == scenario["destination_id"]
                    )
                )
            ).scalar_one()
            async with (
                CommPeakSource(scenario["src"]) as src,
                WasabiDestination(scenario["dst"]) as dst,
            ):
                outcome = await transfer_recording(
                    s,
                    rec,
                    dest_row,
                    src,
                    dst,
                    brand_slug="go4rex",
                    tenant_slug="go4rex-td",
                    multipart_threshold=16 * 1024 * 1024,
                    multipart_chunk=5 * 1024 * 1024,
                )
            await s.commit()

        # Simulate archive-side loss, which nightly reconciliation must catch.
        async with S3Client(scenario["dst"]) as c:
            await c.client.delete_object(Bucket=scenario["dst"].bucket, Key=outcome.destination_key)

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            async with WasabiDestination(scenario["dst"]) as dst:
                ok = await verify_recording(s, rec, dst)
            await s.commit()

        assert ok is False
        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = (
                await s.execute(
                    select(Recording).where(
                        Recording.source_key == SRC_KEY,
                        Recording.connection_id == scenario["connection_id"],
                    )
                )
            ).scalar_one()
            assert rec.state is RecordingState.QUEUED, "a lost archive copy must be re-transferred"


class TestQueue:
    async def test_claim_is_exclusive_across_workers(self, db, scenario):
        """Two workers polling at once must never claim the same job."""
        async with await _scoped(db, scenario["brand_id"]) as s:
            recs = []
            for n in range(6):
                rec = Recording(
                    brand_id=scenario["brand_id"],
                    connection_id=scenario["connection_id"],
                    tenant_id=1,
                    source_key=f"2026/09/08/00/q-{uuid.uuid4().hex}-{n}.flac",
                    source_size=10,
                    state=RecordingState.QUEUED,
                )
                s.add(rec)
                await s.flush()
                recs.append(rec)
                await queue.enqueue(
                    s,
                    brand_id=scenario["brand_id"],
                    connection_id=scenario["connection_id"],
                    recording_id=rec.id,
                    destination_id=scenario["destination_id"],
                    priority=100,
                )
            await s.commit()

        s1 = await _scoped(db, scenario["brand_id"])
        s2 = await _scoped(db, scenario["brand_id"])
        try:
            batch1 = await queue.claim_batch(s1, worker="w1", limit=3, lease_seconds=900)
            batch2 = await queue.claim_batch(s2, worker="w2", limit=3, lease_seconds=900)
            ids1 = {j.id for j in batch1}
            ids2 = {j.id for j in batch2}
            assert len(ids1) == 3
            assert len(ids2) == 3
            assert not (ids1 & ids2), "SKIP LOCKED must hand each job to exactly one worker"
            await s1.commit()
            await s2.commit()
        finally:
            await s1.close()
            await s2.close()

    async def test_enqueue_is_idempotent(self, db, scenario):
        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = Recording(
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                tenant_id=1,
                source_key=f"2026/09/08/00/dup-{uuid.uuid4().hex}.flac",
                source_size=1,
                state=RecordingState.QUEUED,
            )
            s.add(rec)
            await s.flush()
            for _ in range(3):
                await queue.enqueue(
                    s,
                    brand_id=scenario["brand_id"],
                    connection_id=scenario["connection_id"],
                    recording_id=rec.id,
                    destination_id=scenario["destination_id"],
                )
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            n = (
                await s.execute(
                    select(text("count(*)"))
                    .select_from(TransferJob)
                    .where(TransferJob.recording_id == rec.id)
                )
            ).scalar_one()
            assert n == 1

    async def test_expired_lease_is_reclaimed(self, db, scenario):
        """A worker killed mid-transfer must not strand its job."""
        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = Recording(
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                tenant_id=1,
                source_key=f"2026/09/08/00/lease-{uuid.uuid4().hex}.flac",
                source_size=1,
                state=RecordingState.QUEUED,
            )
            s.add(rec)
            await s.flush()
            await queue.enqueue(
                s,
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                recording_id=rec.id,
                destination_id=scenario["destination_id"],
            )
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            claimed = await queue.claim_batch(s, worker="doomed", limit=1, lease_seconds=900)
            job_id = claimed[0].id
            rec.state = RecordingState.TRANSFERRING
            # Pretend the worker died twenty minutes ago.
            await s.execute(
                text(
                    "UPDATE transfer_jobs SET claimed_at = now() - interval '20 minutes' "
                    "WHERE id = :i"
                ),
                {"i": job_id},
            )
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            reclaimed = await queue.reclaim_expired(s, lease_seconds=900)
            await s.commit()
            assert reclaimed >= 1

        async with await _scoped(db, scenario["brand_id"]) as s:
            job = (
                await s.execute(select(TransferJob).where(TransferJob.id == job_id))
            ).scalar_one()
            assert job.state is JobState.PENDING
            assert job.claimed_by is None

    async def test_failure_retries_then_gives_up(self, db, scenario):
        from c2w.storage.errors import ErrorClass

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = Recording(
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                tenant_id=1,
                source_key=f"2026/09/08/00/fail-{uuid.uuid4().hex}.flac",
                source_size=1,
                state=RecordingState.QUEUED,
            )
            s.add(rec)
            await s.flush()
            await queue.enqueue(
                s,
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                recording_id=rec.id,
                destination_id=scenario["destination_id"],
            )
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            job = (await queue.claim_batch(s, worker="w", limit=1, lease_seconds=900))[0]
            will_retry = await queue.fail(
                s,
                job,
                error_class=ErrorClass.NETWORK_ERROR,
                detail="uplink dropped",
                max_attempts=3,
                ladder=[30, 120],
            )
            assert will_retry, "a network blip must be retried"
            assert job.state is JobState.PENDING
            assert job.next_attempt_at > datetime.now(UTC)
            await s.commit()

    async def test_auth_errors_are_not_retried(self, db, scenario):
        """Retrying AccessDenied five times only delays the alert a human needs."""
        from c2w.storage.errors import ErrorClass

        async with await _scoped(db, scenario["brand_id"]) as s:
            rec = Recording(
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                tenant_id=1,
                source_key=f"2026/09/08/00/acl-{uuid.uuid4().hex}.flac",
                source_size=1,
                state=RecordingState.QUEUED,
            )
            s.add(rec)
            await s.flush()
            await queue.enqueue(
                s,
                brand_id=scenario["brand_id"],
                connection_id=scenario["connection_id"],
                recording_id=rec.id,
                destination_id=scenario["destination_id"],
            )
            await s.commit()

        async with await _scoped(db, scenario["brand_id"]) as s:
            job = (await queue.claim_batch(s, worker="w", limit=1, lease_seconds=900))[0]
            will_retry = await queue.fail(
                s,
                job,
                error_class=ErrorClass.ACL_ERROR,
                detail="IP not whitelisted",
                max_attempts=5,
                ladder=[30, 120],
            )
            assert will_retry is False
            assert job.state is JobState.FAILED
            await s.commit()


class TestPriorityAndScheduling:
    def test_recent_recordings_outrank_history(self):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        recent = backfill_priority(now - timedelta(hours=6), now=now)
        month = backfill_priority(now - timedelta(days=20), now=now)
        old = backfill_priority(now - timedelta(days=900), now=now)
        assert recent < month < old, "newest recordings must drain first"
        assert backfill_priority(None, now=now) == old

    def test_incremental_window_overlaps_the_cursor(self):
        """A call starting at 10:59 lands in the 10:00 prefix long after that
        hour passed, so the window must reach back."""
        conn = CommPeakConnection(
            brand_id=1,
            tenant_id=1,
            name="c",
            s3_bucket="b",
            s3_access_key_sealed="x",
            s3_secret_sealed="x",
        )
        now = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)
        conn.inventory_cursor_hour = datetime(2026, 9, 8, 11, tzinfo=UTC)
        start, end = plan_incremental(conn, overlap_hours=3, now=now)
        assert start == datetime(2026, 9, 8, 8, tzinfo=UTC)
        assert end == datetime(2026, 9, 8, 12, tzinfo=UTC)

    def test_never_scanned_connection_does_not_walk_all_history(self):
        """An incremental poll must not accidentally start a 12.9M-object scan."""
        conn = CommPeakConnection(
            brand_id=1,
            tenant_id=1,
            name="c",
            s3_bucket="b",
            s3_access_key_sealed="x",
            s3_secret_sealed="x",
        )
        now = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)
        start, end = plan_incremental(conn, overlap_hours=3, now=now)
        assert (end - start) <= timedelta(hours=3)


class TestSidecar:
    def test_sidecar_describes_the_call_without_the_database(self):
        rec = Recording(
            brand_id=1,
            connection_id=1,
            tenant_id=1,
            source_key=SRC_KEY,
            source_size=1234,
            checksum_sha256="abc",
            call_uuid="e9a46b6f-4711-4225-a731-bf338712817d",
            match_method="epoch_exact",
            match_confidence=0.99,
            started_at=datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC),
            uniqueid=UNIQUEID,
            seq=0,
            file_ext="flac",
        )
        import json

        payload = json.loads(build_sidecar(rec, {"dst": "0007281"}))
        assert payload["recording"]["source_key"] == SRC_KEY
        assert payload["correlation"]["call_uuid"] == rec.call_uuid
        assert payload["cdr"]["dst"] == "0007281"
