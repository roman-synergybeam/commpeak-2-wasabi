"""End-to-end pipeline: discover -> correlate -> queue -> transfer -> verify.

Runs against a real PostgreSQL (for the queue's SKIP LOCKED semantics and the
partitioned tables) and a real moto S3 server standing in for both CommPeak and
the archive.  Mocking either would hide precisely the behaviour that matters:
concurrent job claims, multipart assembly, and read-back verification.

Skipped unless C2W_TEST_DATABASE_URL is set; see tests/test_brand_isolation.py.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.db.base import JobKind, JobState, RecordingState
from c2w.db.models.core import CommPeakConnection, Recording, StorageDestination, TransferJob
from c2w.storage.commpeak import CommPeakSource
from c2w.storage.s3_adapter import S3Client
from c2w.storage.wasabi import WasabiDestination
from c2w.sync import queue
from c2w.sync.inventory import backfill_priority, plan_incremental, scan_day
from c2w.sync.transfer import build_sidecar, transfer_recording, verify_recording

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)

DAY = datetime(2026, 9, 8, tzinfo=UTC)
# Channel id whose epoch is exactly the call start, as CommPeak names them.
UNIQUEID = int(datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC).timestamp())
# The real layout: a `recordings/` root and no hour folder. Seeding the
# documented shape instead let the scanner pass against a prefix that
# exists nowhere in a live bucket.
SRC_KEY = f"recordings/2026/09/08/out-593990899917-101-20260908-005217-{UNIQUEID}.0.flac"


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
        #
        # Scoped to this brand, not TRUNCATE. `TRUNCATE cdrs` on a partitioned
        # parent empties *every* brand's partition, so this fixture used to
        # delete other organisations' calls and recordings as a side effect --
        # harmless in a throwaway database, and destructive in any database
        # somebody else is also using. Setting the brand first means RLS
        # narrows these deletes even if the WHERE were ever dropped.
        await s.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )
        for table in ("transfer_attempts", "transfer_jobs", "recordings", "cdrs"):
            await s.execute(
                text(f"DELETE FROM {table} WHERE brand_id = :b"),  # noqa: S608 - fixed names
                {"b": brand_id},
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
        await _seed_recording(scenario["src"], "recordings/2026/09/08/notes.json", b"{}")

        async with await _scoped(db, scenario["brand_id"]) as s:
            conn = (
                await s.execute(
                    select(CommPeakConnection).where(
                        CommPeakConnection.id == scenario["connection_id"]
                    )
                )
            ).scalar_one()
            async with CommPeakSource(scenario["src"]) as src:
                result = await scan_day(
                    s, src, conn, DAY, destination_id=scenario["destination_id"]
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
                first = await scan_day(
                    s, src, conn, DAY, destination_id=scenario["destination_id"]
                )
                second = await scan_day(
                    s, src, conn, DAY, destination_id=scenario["destination_id"]
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
                result = await scan_day(s, src, conn, DAY, destination_id=None)
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
                await scan_day(s, src, conn, DAY, destination_id=scenario["destination_id"])
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
                    account="go4rex.td",
                    multipart_threshold=16 * 1024 * 1024,
                    multipart_chunk=5 * 1024 * 1024,
                )
            await s.commit()

        assert outcome.verified
        assert outcome.bytes_transferred == len(audio)
        assert outcome.sidecar_written
        # Brand and tenant lead the key so a brand's objects stay contiguous.
        # The account folder leads, and the source key is preserved verbatim
        # underneath -- including CommPeak's own recordings/ tree.
        assert outcome.destination_key == f"archive/go4rex.td/{SRC_KEY}"

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
                await scan_day(s, src, conn, DAY, destination_id=scenario["destination_id"])
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
                    account="go4rex.td",
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
                await scan_day(s, src, conn, DAY, destination_id=scenario["destination_id"])
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
                    account="go4rex.td",
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
                await scan_day(s, src, conn, DAY, destination_id=scenario["destination_id"])
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
                    account="go4rex.td",
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
        conn.inventory_cursor_day = datetime(2026, 9, 8, tzinfo=UTC)
        # Days now, and the overlap is rounded up to whole days: 3 hours means
        # "also re-list the day before", not "round down to nothing".
        start, end = plan_incremental(conn, overlap_hours=3, now=now)
        assert start == datetime(2026, 9, 7, tzinfo=UTC)
        assert end == datetime(2026, 9, 8, tzinfo=UTC)

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
        assert (end - start) <= timedelta(days=1)


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


@contextlib.asynccontextmanager
async def _scoped_platform_session(db, brand_id):
    """A stand-in for `platform_session`, scoped to one organisation.

    The real one connects as a BYPASSRLS role so a worker can read across
    organisations. Tests run as the ordinary application role, where RLS is
    forced and no brand is set, so the real call would see an empty database
    and every assertion below would pass for the wrong reason. Scoping it
    instead keeps these tests about the watch's own logic -- which account it
    picks, when it alerts -- and leaves "is the platform role wired up" to the
    deployment, where it is a privilege question rather than a code one.
    """
    async with db() as session:
        await session.execute(
            text("SELECT set_config('c2w.brand_id', :b, false)"), {"b": str(brand_id)}
        )
        yield session
        await session.commit()



class TestWatchingForAccessToComeBack:
    """The scheduler re-checks failing accounts and announces recovery.

    This replaces a person on the settings page pressing Test, which is how
    the source came to refuse every account in the first place: roughly twenty
    checks in half an hour tripped its rate limit, and a rate-limited refusal
    is byte-for-byte the refusal an unlisted address gets. So the pacing here
    is the feature, not an implementation detail.
    """

    async def test_it_checks_one_account_per_turn_oldest_first(self, db, scenario):
        """Eight failing accounts must not become eight requests at once."""
        from c2w.db.base import ConnectionStatus
        from c2w.workers.scheduler import Scheduler

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            tenant_id = (
                await s.execute(
                    text(
                        "SELECT tenant_id FROM commpeak_connections WHERE id = :i"
                    ),
                    {"i": scenario["connection_id"]},
                )
            ).scalar_one()
            # Three failing accounts, checked at distinct times.
            ids = []
            for minutes in (30, 10, 20):
                conn = CommPeakConnection(
                    brand_id=scenario["brand_id"],
                    tenant_id=tenant_id,
                    name=f"watch-{minutes}",
                    s3_endpoint="http://127.0.0.1:1",
                    s3_region="us-east-1",
                    s3_bucket=str(uuid.uuid4()),
                    s3_access_key_sealed="x",
                    s3_secret_sealed="x",
                    status=ConnectionStatus.ERROR,
                    last_probe_at=datetime.now(UTC) - timedelta(minutes=minutes),
                )
                s.add(conn)
                await s.flush()
                ids.append((minutes, conn.id))
            await s.commit()

        tried: list[int] = []

        async def _fake_open_source(session, connection):
            tried.append(connection.id)
            raise OSError("refused")

        import c2w.workers.scheduler as sched

        original = sched.open_source
        original_session = sched.platform_session
        sched.open_source = _fake_open_source
        sched.platform_session = lambda: _scoped_platform_session(
            db, scenario["brand_id"]
        )
        try:
            await Scheduler()._watch_access()
        finally:
            sched.open_source = original
            sched.platform_session = original_session

        assert len(tried) == 1, "more than one account was checked in a single turn"
        oldest = next(cid for minutes, cid in ids if minutes == 30)
        assert tried[0] == oldest, "the least recently checked account was not chosen"

    async def test_recovery_flips_the_status_and_sends_one_alert(self, db, scenario):
        """The alert is on the transition, not on the state."""
        from c2w.db.base import ConnectionStatus
        from c2w.workers.scheduler import Scheduler

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            tenant_id = (
                await s.execute(
                    text("SELECT tenant_id FROM commpeak_connections WHERE id = :i"),
                    {"i": scenario["connection_id"]},
                )
            ).scalar_one()
            # Only this account should be failing, or the watch legitimately
            # recovers a leftover from another test in the same organisation
            # and the count below is measuring the wrong thing.
            await s.execute(
                text(
                    "UPDATE commpeak_connections SET status = 'OK' "
                    "WHERE brand_id = :b"
                ),
                {"b": scenario["brand_id"]},
            )
            conn = CommPeakConnection(
                brand_id=scenario["brand_id"],
                tenant_id=tenant_id,
                name="comes-back",
                s3_endpoint="http://127.0.0.1:1",
                s3_region="us-east-1",
                s3_bucket=str(uuid.uuid4()),
                s3_access_key_sealed="x",
                s3_secret_sealed="x",
                status=ConnectionStatus.ERROR,
                status_detail="Forbidden",
                last_probe_at=datetime.now(UTC) - timedelta(hours=5),
            )
            s.add(conn)
            await s.flush()
            conn_id = conn.id
            await s.commit()

        class _Src:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def list_common_prefixes(self, _prefix, delimiter="/"):
                return ["2026/"]

        async def _fake_open_source(_session, _connection):
            return _Src()

        sent: list[object] = []

        async def _fake_dispatch(_session, alert):
            sent.append(alert)
            return ["telegram"]

        import c2w.workers.scheduler as sched

        open_original, dispatch_original = sched.open_source, sched.dispatch
        session_original = sched.platform_session
        sched.open_source = _fake_open_source
        sched.dispatch = _fake_dispatch
        sched.platform_session = lambda: _scoped_platform_session(
            db, scenario["brand_id"]
        )
        try:
            await Scheduler()._watch_access()
            # A second turn must not re-announce: the account is OK now, so the
            # watch does not even look at it.
            await Scheduler()._watch_access()
        finally:
            sched.open_source = open_original
            sched.dispatch = dispatch_original
            sched.platform_session = session_original

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            row = (
                await s.execute(
                    select(CommPeakConnection).where(CommPeakConnection.id == conn_id)
                )
            ).scalar_one()
            assert row.status == ConnectionStatus.OK
            assert row.status_detail is None

        assert len(sent) == 1, f"expected exactly one alert, got {len(sent)}"
        assert "working again" in sent[0].title
        assert "comes-back" in sent[0].body

    async def test_inventory_leaves_failing_accounts_to_the_watch(self, db, scenario):
        """Ninety-six refused requests an hour is what kept the block alive."""
        from c2w.workers.scheduler import Scheduler

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            await s.execute(
                text(
                    "UPDATE commpeak_connections SET status = 'ERROR' WHERE id = :i"
                ),
                {"i": scenario["connection_id"]},
            )
            await s.commit()

        scanned: list[int] = []

        async def _record(_self, connection_id):
            scanned.append(connection_id)

        import c2w.workers.scheduler as sched

        original = sched.Scheduler._scan_connection
        session_original = sched.platform_session
        sched.Scheduler._scan_connection = _record
        sched.platform_session = lambda: _scoped_platform_session(
            db, scenario["brand_id"]
        )
        try:
            await Scheduler()._run_incremental_inventory()
        finally:
            sched.Scheduler._scan_connection = original

        assert scenario["connection_id"] not in scanned
        # And it is picked up again once the watch has cleared it.
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            await s.execute(
                text("UPDATE commpeak_connections SET status = 'OK' WHERE id = :i"),
                {"i": scenario["connection_id"]},
            )
            await s.commit()

        scanned.clear()
        sched.Scheduler._scan_connection = _record
        try:
            await Scheduler()._run_incremental_inventory()
        finally:
            sched.Scheduler._scan_connection = original
            sched.platform_session = session_original
        assert scenario["connection_id"] in scanned


class TestTheArchiveIsArrangedByAccount:
    """The bucket's top level must read as the list of CommPeak accounts.

    Asked for directly: folders named `go4rex.pbx`, `go4rex.td`,
    `go4rexnew.td` and so on, matching what they are called at CommPeak.
    """

    def test_the_account_name_leads_the_key(self):
        from c2w.storage.wasabi import destination_key

        key = destination_key(
            path_prefix="archive",
            account="go4rex.pbx",
            source_key="recordings/2026/09/08/12/1788871734.100994-out-1-201-20260908-124856.flac",
        )
        assert key == (
            "archive/go4rex.pbx/"
            "recordings/2026/09/08/12/1788871734.100994-out-1-201-20260908-124856.flac"
        )

    def test_no_brand_folder_is_inserted(self):
        """Each organisation has its own bucket, so a brand level says nothing.

        It also pushed the account names one deeper than asked for.
        """
        from c2w.storage.wasabi import destination_key

        key = destination_key(
            path_prefix="", account="intermagnum.td", source_key="recordings/2026/09/08/x.flac"
        )
        assert key == "intermagnum.td/recordings/2026/09/08/x.flac"
        assert "go4rex" not in key and "intermagnum/" not in key

    def test_the_source_path_is_preserved_verbatim(self):
        """An archived object must be traceable without the database."""
        from c2w.storage.wasabi import destination_key

        source = "recordings/2022/12/26/in-99150321131757-503-20221226-152523-1672068323.9.flac"
        key = destination_key(path_prefix="archive", account="go4rex.td", source_key=source)
        assert key.endswith(source)

    def test_dots_survive_but_path_tricks_do_not(self):
        """`go4rex.pbx` must stay readable; `../` must not become a path."""
        from c2w.storage.wasabi import account_folder

        assert account_folder("go4rex.pbx") == "go4rex.pbx"
        assert account_folder("verificationgo4rex.td") == "verificationgo4rex.td"
        # Operator-entered free text ends up in an object key.
        assert "/" not in account_folder("a/b/c")
        assert account_folder("../../etc") == "etc"
        assert not account_folder("...").startswith(".")
        assert account_folder("   ") == "unnamed-account"

    def test_the_tenant_slug_is_not_used(self):
        """The slug is not the account name, and collides.

        `go4rex.pbx` really has the tenant slug `go4rex-2` in production --
        slugs get a counter appended on collision -- so archiving by slug
        produced a folder nobody could identify.
        """
        from pathlib import Path

        body = Path("src/c2w/storage/wasabi.py").read_text()
        assert "tenant_slug" not in body


class TestTheSyncSummaryAlert:
    """A periodic message saying what actually moved.

    Two of its numbers were wrong when first written, and both wrongnesses
    are the interesting part:

    * **Copied** read `sync_runs.transferred`, which only *inventory* writes --
      never the worker. So it reported "Copied: 0" while 485 recordings sat
      verified in the archive. That is the single figure the message exists to
      carry. It now counts recordings whose `verified_at` falls in the window,
      which is the only moment "copied" is actually true.
    * **Waiting** counted only `DISCOVERED`, and a recording with a job is
      `QUEUED`. It reported 0 while 54,525 were outstanding.
    """

    async def _summary(self, db, brand_id, *, minutes=60):
        import c2w.workers.scheduler as sched
        from c2w.workers.scheduler import Scheduler

        sent = []

        async def _spy(_session, alert):
            sent.append(alert)
            return ["telegram"]

        async def _scoped():
            return _scoped_platform_session(db, brand_id)

        original_dispatch, original_session = sched.dispatch, sched.platform_session
        sched.dispatch = _spy
        sched.platform_session = lambda: _scoped_platform_session(db, brand_id)
        try:
            await Scheduler()._send_sync_summary(minutes)
        finally:
            sched.dispatch = original_dispatch
            sched.platform_session = original_session
        return sent

    async def _seed_one(self, db, scenario, *, state, verified):
        """One recording in a known state. The fixture creates none."""
        from datetime import UTC, datetime

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            tenant_id = (
                await s.execute(
                    text("SELECT tenant_id FROM commpeak_connections WHERE id = :i"),
                    {"i": scenario["connection_id"]},
                )
            ).scalar_one()
            await s.execute(
                text(
                    "INSERT INTO recordings (brand_id, connection_id, tenant_id, "
                    "source_key, source_size, state, verified_at, destination_size) "
                    "VALUES (:b, :c, :t, :k, 1000, :st, :v, 1000)"
                ),
                {
                    "b": scenario["brand_id"],
                    "c": scenario["connection_id"],
                    "t": tenant_id,
                    "k": f"recordings/2026/09/08/{uuid.uuid4().hex}.flac",
                    "st": state,
                    "v": datetime.now(UTC) if verified else None,
                },
            )
            await s.commit()

    async def test_it_counts_a_verified_recording_as_copied(self, db, scenario):
        await self._seed_one(db, scenario, state="AVAILABLE", verified=True)

        sent = await self._summary(db, scenario["brand_id"])
        mine = [a for a in sent if a.brand_id == scenario["brand_id"]]
        assert mine, "no summary was sent for this organisation"
        copied = mine[0].fields["Copied"]
        # The regression: this said "0" while the archive held the rows.
        assert not copied.startswith("0 "), copied

    async def test_a_queued_recording_counts_as_waiting(self, db, scenario):
        await self._seed_one(db, scenario, state="QUEUED", verified=False)

        sent = await self._summary(db, scenario["brand_id"])
        mine = [a for a in sent if a.brand_id == scenario["brand_id"]]
        assert mine
        assert not mine[0].fields["Waiting to copy"].startswith("0")

    async def test_it_names_every_account_and_how_far_it_has_scanned(
        self, db, scenario
    ):
        """"With details" was the request: per account, not one total."""
        sent = await self._summary(db, scenario["brand_id"])
        mine = [a for a in sent if a.brand_id == scenario["brand_id"]]
        assert mine
        assert "Go4Rex TD" in mine[0].body or "scanned" in mine[0].body

    async def test_it_still_reports_when_nothing_moved(self, db, scenario):
        """Silence cannot distinguish an idle system from a stopped one."""
        sent = await self._summary(db, scenario["brand_id"])
        assert [a for a in sent if a.brand_id == scenario["brand_id"]]

    async def test_each_window_is_its_own_alert_not_a_duplicate(self, db, scenario):
        """Deduplication must not swallow a periodic report."""
        first = await self._summary(db, scenario["brand_id"], minutes=60)
        second = await self._summary(db, scenario["brand_id"], minutes=30)
        keys = {a.dedupe_key for a in first + second if a.brand_id == scenario["brand_id"]}
        assert len(keys) >= 1
        assert all(str(scenario["brand_id"]) in k for k in keys)


class TestTheClaimSpreadsAcrossAccounts:
    """Every account with work gets a share of each pass.

    Measured on the live backlog before this: five accounts holding 120,000
    queued jobs were idle while everything piled onto one. Inventory enqueues
    an account's objects in bulk, so a single claim ordered by
    `priority, next_attempt_at` returns consecutive jobs -- all from the same
    account -- and the per-account cap then runs them one at a time. Eight
    accounts at five concurrent each is a budget of forty; about five were in
    use.
    """

    async def _two_accounts(self, db, scenario):
        """A second account in the same organisation, with jobs on both."""
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            tenant_id = (
                await s.execute(
                    text("SELECT tenant_id FROM commpeak_connections WHERE id = :i"),
                    {"i": scenario["connection_id"]},
                )
            ).scalar_one()
            other = CommPeakConnection(
                brand_id=scenario["brand_id"],
                tenant_id=tenant_id,
                name=f"second-{uuid.uuid4().hex[:6]}",
                s3_endpoint="http://127.0.0.1:1",
                s3_region="us-east-1",
                s3_bucket=str(uuid.uuid4()),
                s3_access_key_sealed="x",
                s3_secret_sealed="x",
            )
            s.add(other)
            await s.flush()

            # A big backlog on the first account and a small one on the second:
            # the shape that starved the small account.
            for conn_id, count in ((scenario["connection_id"], 40), (other.id, 3)):
                for _ in range(count):
                    rec_id = (
                        await s.execute(
                            text(
                                "INSERT INTO recordings (brand_id, connection_id, "
                                "tenant_id, source_key, source_size, state) VALUES "
                                "(:b, :c, :t, :k, 10, 'QUEUED') RETURNING id"
                            ),
                            {
                                "b": scenario["brand_id"],
                                "c": conn_id,
                                "t": tenant_id,
                                "k": f"recordings/2026/09/08/{uuid.uuid4().hex}.flac",
                            },
                        )
                    ).scalar_one()
                    await s.execute(
                        text(
                            "INSERT INTO transfer_jobs (brand_id, recording_id, "
                            "connection_id, kind, state, priority, next_attempt_at) "
                            "VALUES (:b, :r, :c, 'TRANSFER', 'PENDING', 100, now())"
                        ),
                        {"b": scenario["brand_id"], "r": rec_id, "c": conn_id},
                    )
            await s.commit()
            return scenario["connection_id"], other.id

    async def test_a_small_account_is_not_starved_by_a_large_one(self, db, scenario):
        from c2w.sync import queue

        big, small = await self._two_accounts(db, scenario)

        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            from c2w.workers.worker import Worker

            order = await Worker._connections_with_work(s)
            # Fewest queued first, so the small account is claimed from before
            # the large one rather than behind 40 of its jobs.
            assert order.index(small) < order.index(big), order

            claimed = []
            for conn_id in order:
                got = await queue.claim_batch(
                    s,
                    worker="test",
                    limit=2,
                    lease_seconds=60,
                    kinds=[JobKind.TRANSFER],
                    connection_ids=[conn_id],
                )
                claimed.extend(got)
            await s.commit()

        accounts = {j.connection_id for j in claimed}
        assert small in accounts, "the small account got nothing"
        assert big in accounts, "the large account got nothing"

    async def test_narrowing_to_one_account_still_claims_exclusively(self, db, scenario):
        """The property the whole queue rests on must survive the new filter.

        Two workers asking for the same account must get disjoint sets. This
        is why the ranking is not done inside the claim: a window function
        cannot share a SELECT with `FOR UPDATE`, and moving the lock outward
        makes `SKIP LOCKED` stop skipping during selection -- the second worker
        then re-picks the same head rows and comes back empty.
        """
        from c2w.sync import queue

        big, _small = await self._two_accounts(db, scenario)

        async with db() as s1, db() as s2:
            for s in (s1, s2):
                await s.execute(
                    text("SELECT set_config('c2w.brand_id', :b, false)"),
                    {"b": str(scenario["brand_id"])},
                )
            first = await queue.claim_batch(
                s1, worker="w1", limit=3, lease_seconds=60,
                kinds=[JobKind.TRANSFER], connection_ids=[big],
            )
            second = await queue.claim_batch(
                s2, worker="w2", limit=3, lease_seconds=60,
                kinds=[JobKind.TRANSFER], connection_ids=[big],
            )
            assert len(first) == 3, len(first)
            assert len(second) == 3, len(second)
            assert not ({j.id for j in first} & {j.id for j in second})
            await s1.rollback()
            await s2.rollback()

    async def test_an_account_with_no_work_is_not_asked(self, db, scenario):
        from c2w.workers.worker import Worker

        _big, _small = await self._two_accounts(db, scenario)
        async with db() as s:
            await s.execute(
                text("SELECT set_config('c2w.brand_id', :b, false)"),
                {"b": str(scenario["brand_id"])},
            )
            await s.execute(text("UPDATE transfer_jobs SET state = 'DONE'"))
            await s.commit()
            assert await Worker._connections_with_work(s) == []


class TestTheScannerBoundsItsTransaction:
    """A day is not a safe unit of work to hold locks for.

    One real day on `intermagnum.td` holds 124,784 objects. Inserting them in
    a single transaction holds row locks on tens of thousands of recordings and
    jobs, while the workers lock the same rows in the opposite order -- they
    take a job then update its recording; the scanner inserts the recording
    then its job. That is a deadlock by arrangement rather than by luck, and it
    killed two backfill runs.
    """

    def test_scan_day_commits_in_slices_by_default(self):
        import inspect

        from c2w.sync.inventory import scan_day

        default = inspect.signature(scan_day).parameters["commit_every"].default
        assert isinstance(default, int)
        assert 0 < default <= 2000, default

    def test_scan_range_passes_the_slice_through(self):
        """Otherwise the bound exists and the backfill never uses it."""
        import inspect

        from c2w.sync.inventory import scan_range

        assert "commit_every" in inspect.signature(scan_range).parameters
        source = inspect.getsource(scan_range)
        assert "commit_every=commit_every" in source

    async def test_a_partly_scanned_day_leaves_the_cursor_alone(self, db, scenario):
        """Committing part-way through must not claim the day is done.

        The cursor is what an incremental pass resumes from, so advancing it
        for a day that only partly landed would skip the rest for ever.
        """
        import inspect

        from c2w.sync.inventory import scan_range

        source = inspect.getsource(scan_range)
        cursor_line = next(
            line for line in source.splitlines() if "inventory_cursor_day" in line
        )
        # Set after the day's scan returns, not inside it.
        assert source.index("await scan_day(") < source.index(cursor_line)
