"""Streaming transfer, checksum verification and multipart behaviour."""

from __future__ import annotations

import hashlib
import os

import pytest

from cprec.storage.base import ObjectRef
from cprec.storage.errors import ErrorClass, TransferError
from cprec.storage.s3_adapter import RateLimiter, S3Client


async def _seed(creds, key: str, data: bytes) -> None:
    async with S3Client(creds) as c:
        await c.client.put_object(Bucket=creds.bucket, Key=key, Body=data)


async def test_probe_ok(bucket):
    async with S3Client(bucket) as c:
        await c.probe()


async def test_probe_missing_bucket(s3_creds):
    async with S3Client(s3_creds("definitely-absent-bucket")) as c:
        with pytest.raises(TransferError) as exc:
            await c.probe()
    assert exc.value.error_class is ErrorClass.CONFIG_ERROR


async def test_list_prefix_paginates_and_resumes(bucket):
    keys = [
        f"2026/09/08/00/out-1000{i:02d}-101-20260908-000000-175729{i:04d}.0.flac"
        for i in range(25)
    ]
    for k in keys:
        await _seed(bucket, k, b"x")

    async with S3Client(bucket) as c:
        seen = [ref.key async for ref in c.list_prefix("2026/09/08/00/", page_size=7)]
        assert seen == sorted(keys), "listing must be complete and key-ordered across pages"

        resumed = [
            ref.key async for ref in c.list_prefix("2026/09/08/00/", start_after=sorted(keys)[9])
        ]
        assert resumed == sorted(keys)[10:], "start_after must resume without re-reading"


async def test_stream_roundtrip_small_object(bucket, s3_creds):
    """Small objects take the single-shot path and still get a real digest."""
    data = os.urandom(64 * 1024)
    await _seed(bucket, "src/small.flac", data)
    dest = s3_creds(f"{bucket.bucket}-dest")
    async with S3Client(dest) as d:
        await d.client.create_bucket(Bucket=dest.bucket)

    async with S3Client(bucket) as src, S3Client(dest) as dst:
        meta = await src.head("src/small.flac")
        result = await dst.put_stream(
            "arch/small.flac", src.open_stream("src/small.flac"), size_hint=meta.size
        )

    assert result.multipart is False
    assert result.size == len(data)
    assert result.checksum_sha256 == hashlib.sha256(data).hexdigest()

    async with S3Client(dest) as d:
        head = await d.head("arch/small.flac")
        assert head.size == len(data)
        assert head.checksum_sha256 == result.checksum_sha256, "digest must survive as metadata"


async def test_stream_roundtrip_multipart(bucket, s3_creds):
    """A >threshold object must upload as multipart and stay byte-identical."""
    data = os.urandom(21 * 1024 * 1024)
    await _seed(bucket, "src/big.flac", data)
    dest = s3_creds(f"{bucket.bucket}-mp")
    async with S3Client(dest) as d:
        await d.client.create_bucket(Bucket=dest.bucket)

    async with (
        S3Client(bucket) as src,
        S3Client(dest, multipart_threshold=8 * 1024 * 1024, multipart_chunk=5 * 1024 * 1024) as dst,
    ):
        meta = await src.head("src/big.flac")
        result = await dst.put_stream(
            "arch/big.flac", src.open_stream("src/big.flac"), size_hint=meta.size
        )
        assert result.multipart is True
        assert result.parts >= 4
        assert result.size == len(data)
        assert result.checksum_sha256 == hashlib.sha256(data).hexdigest()

        # Read the archived object back in full and compare bytes, not just size.
        got = bytearray()
        async for chunk in dst.open_stream("arch/big.flac"):
            got.extend(chunk)
        assert bytes(got) == data, "archived object must be byte-identical to the source"


async def test_range_resume_reads_tail(bucket):
    data = os.urandom(1024 * 1024)
    await _seed(bucket, "src/resume.flac", data)
    async with S3Client(bucket) as c:
        tail = bytearray()
        async for chunk in c.open_stream("src/resume.flac", offset=1000):
            tail.extend(chunk)
    assert bytes(tail) == data[1000:], "Range request must resume at the requested offset"


async def test_missing_source_object_is_not_found(bucket):
    async with S3Client(bucket) as c:
        with pytest.raises(TransferError) as exc:
            await c.head("no/such.flac")
    assert exc.value.error_class is ErrorClass.NOT_FOUND
    assert exc.value.retryable is False


async def test_presign_get_is_usable(bucket):
    import httpx

    data = b"audio-bytes"
    await _seed(bucket, "src/presign.flac", data)
    async with S3Client(bucket) as c:
        url = await c.presign_get("src/presign.flac", ttl_seconds=60, download_filename="call.flac")
    async with httpx.AsyncClient() as http:
        resp = await http.get(url)
    assert resp.status_code == 200
    assert resp.content == data
    assert "attachment" in resp.headers.get("content-disposition", "")


async def test_multipart_abort_leaves_no_pending_upload(bucket, s3_creds):
    """A failed stream must abort the multipart upload, not leak billable parts."""
    dest = s3_creds(f"{bucket.bucket}-abort")
    async with S3Client(dest) as d:
        await d.client.create_bucket(Bucket=dest.bucket)

    async def exploding():
        yield os.urandom(6 * 1024 * 1024)
        raise ConnectionError("uplink dropped mid-transfer")

    async with S3Client(dest, multipart_threshold=1024, multipart_chunk=5 * 1024 * 1024) as dst:
        with pytest.raises(TransferError) as exc:
            await dst.put_stream("arch/doomed.flac", exploding(), size_hint=50 * 1024 * 1024)
        assert exc.value.error_class is ErrorClass.NETWORK_ERROR
        assert exc.value.retryable is True

        pending = await dst.client.list_multipart_uploads(Bucket=dest.bucket)
        assert not pending.get("Uploads"), "aborted transfer must not leave a pending upload"


async def test_rate_limiter_throttles():
    import time

    limiter = RateLimiter.from_mbps(8)  # 1 MB/s
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire(512 * 1024)
    assert time.monotonic() - start >= 0.4, "limiter must delay once the bucket drains"


async def test_rate_limiter_disabled_is_free():
    limiter = RateLimiter.from_mbps(0)
    assert limiter.enabled is False
    await limiter.acquire(10 * 1024 * 1024)


def test_object_ref_is_hashable():
    from datetime import UTC, datetime

    ref = ObjectRef(key="k", size=1, etag="e", last_modified=datetime(2026, 1, 1, tzinfo=UTC))
    assert {ref, ref} == {ref}


async def test_rate_limiter_admits_oversized_request():
    """A chunk bigger than one second of bandwidth must not deadlock."""
    limiter = RateLimiter.from_mbps(8)  # 1 MB/s, so 1 MB bucket
    await limiter.acquire(4 * 1024 * 1024)


async def test_rate_limiter_average_rate_is_respected():
    import time

    limiter = RateLimiter.from_mbps(80)  # 10 MB/s
    start = time.monotonic()
    for _ in range(6):
        await limiter.acquire(5 * 1024 * 1024)  # 30 MB total
    elapsed = time.monotonic() - start
    # 30 MB at 10 MB/s is ~3 s, less the 1 s the bucket had banked.
    assert 1.5 <= elapsed <= 4.0, f"expected ~2s of throttling, got {elapsed:.2f}s"


class TestSourceIsReadOnly:
    """The CommPeak side must be unwritable, by construction.

    A stray delete against a bucket holding millions of irreplaceable recordings
    cannot be undone, so this is enforced in code rather than by convention.
    """

    async def test_delete_is_refused(self, bucket):
        from cprec.storage.commpeak import CommPeakSource, SourceIsReadOnly

        await _seed(bucket, "2026/09/08/00/out-1-101-20260908-000000-1757292737.0.flac", b"audio")
        async with CommPeakSource(bucket) as src:
            with pytest.raises(SourceIsReadOnly, match="irreversible"):
                await src.delete("2026/09/08/00/out-1-101-20260908-000000-1757292737.0.flac")

            # And the object is still there.
            meta = await src.head("2026/09/08/00/out-1-101-20260908-000000-1757292737.0.flac")
            assert meta.size == 5

    async def test_writes_are_refused(self, bucket):
        from cprec.storage.commpeak import CommPeakSource, SourceIsReadOnly

        async with CommPeakSource(bucket) as src:
            with pytest.raises(SourceIsReadOnly):
                await src.put_bytes("anything.flac", b"x")
            with pytest.raises(SourceIsReadOnly):
                await src.put_stream("anything.flac", None)

    async def test_reads_still_work(self, bucket):
        """Read-only must not mean crippled -- listing and streaming are the job."""
        from cprec.storage.commpeak import CommPeakSource

        key = "2026/09/08/01/in-441632960770-101-20260908-010000-1757296337.0.flac"
        await _seed(bucket, key, b"recording-bytes")
        async with CommPeakSource(bucket) as src:
            listed = [r.key async for r in src.list_prefix("2026/09/08/01/")]
            assert listed == [key]
            got = bytearray()
            async for chunk in src.open_stream(key):
                got.extend(chunk)
            assert bytes(got) == b"recording-bytes"
