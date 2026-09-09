"""aioboto3-backed implementation of the source and destination protocols.

One class serves both roles; ``CommPeakSource`` and ``WasabiDestination`` are
thin, well-named configurations of it (see :mod:`c2w.storage.commpeak` and
:mod:`c2w.storage.wasabi`).

Two behaviours here matter more than the rest:

*Streaming with an in-flight digest.*  Bytes go source -> hash -> destination
without ever landing on local disk, and the SHA-256 falls out of the same pass.
At 13.9 TB across 19M objects, staging to the 84 GB root volume is not an
option, and a second read to compute a checksum would double the egress bill.

*Bandwidth limiting.*  Recordings are pulled from the same uplink that carries
live calls, so the token-bucket limiter is part of the transfer path rather than
an afterthought.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self

import aioboto3
from botocore.config import Config as BotoConfig

from c2w.storage.base import (
    ObjectDestination,
    ObjectMeta,
    ObjectRef,
    ObjectSource,
    S3Credentials,
    UploadResult,
)
from c2w.storage.errors import ErrorClass, TransferError, classify_exception

__all__ = ["RateLimiter", "S3Client", "hashing_stream"]

_DEFAULT_CHUNK = 8 * 1024 * 1024
#: S3 multipart uploads are capped at 10,000 parts.
_MAX_PARTS = 10_000
_MIN_PART_SIZE = 5 * 1024 * 1024


class RateLimiter:
    """Async token bucket, in bytes per second.  ``0`` means unlimited.

    ``burst_seconds`` bounds how much the bucket may bank while idle.  Keeping
    it near one second matters: recordings are pulled over the same uplink that
    carries live calls, and a multi-megabyte burst allowance would let a worker
    saturate the link the instant it wakes up, which is exactly what the cap
    exists to prevent.

    A request larger than the whole bucket is still admitted -- it waits for a
    full bucket and then borrows, letting the balance go negative so the
    following requests pay it back.  Refusing it instead would deadlock any
    transfer whose chunk size exceeds one second of bandwidth.
    """

    def __init__(self, bytes_per_second: float, *, burst_seconds: float = 1.0) -> None:
        self._rate = max(0.0, bytes_per_second)
        self._capacity = max(self._rate * burst_seconds, 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @classmethod
    def from_mbps(cls, mbps: float, *, burst_seconds: float = 1.0) -> RateLimiter:
        """Build from megabits per second, which is how operators think of links."""
        return cls(mbps * 1_000_000 / 8 if mbps > 0 else 0.0, burst_seconds=burst_seconds)

    @property
    def enabled(self) -> bool:
        return self._rate > 0

    async def acquire(self, amount: int) -> None:
        if not self.enabled or amount <= 0:
            return
        needed = min(float(amount), self._capacity)
        async with self._lock:
            while True:
                now = time.monotonic()
                gained = (now - self._updated) * self._rate
                self._tokens = min(self._capacity, self._tokens + gained)
                self._updated = now
                if self._tokens >= needed:
                    self._tokens -= amount
                    return
                await asyncio.sleep(min((needed - self._tokens) / self._rate, 1.0))


async def hashing_stream(
    stream: AsyncIterator[bytes], digest: hashlib._Hash, limiter: RateLimiter | None = None
) -> AsyncIterator[bytes]:
    """Pass bytes through, updating ``digest`` and honouring ``limiter``."""
    async for chunk in stream:
        if not chunk:
            continue
        digest.update(chunk)
        if limiter is not None:
            await limiter.acquire(len(chunk))
        yield chunk


@dataclass(slots=True)
class _PartBuffer:
    """Accumulates streamed chunks into multipart-sized parts."""

    target: int
    buf: bytearray

    def add(self, chunk: bytes) -> list[bytes]:
        self.buf.extend(chunk)
        out: list[bytes] = []
        while len(self.buf) >= self.target:
            out.append(bytes(self.buf[: self.target]))
            del self.buf[: self.target]
        return out

    def flush(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


class S3Client(ObjectSource, ObjectDestination):
    """S3-compatible client fulfilling both storage roles.

    Use as an async context manager so the underlying aiobotocore session and
    connection pool are closed deterministically::

        async with S3Client(creds) as s3:
            await s3.probe()
    """

    def __init__(
        self,
        credentials: S3Credentials,
        *,
        limiter: RateLimiter | None = None,
        multipart_threshold: int = 16 * 1024 * 1024,
        multipart_chunk: int = 16 * 1024 * 1024,
        max_pool_connections: int = 25,
        connect_timeout: int = 15,
        read_timeout: int = 120,
    ) -> None:
        self.creds = credentials
        self.limiter = limiter
        self.multipart_threshold = multipart_threshold
        self.multipart_chunk = max(multipart_chunk, _MIN_PART_SIZE)
        self._boto_config = BotoConfig(
            signature_version=credentials.signature_version,
            s3={"addressing_style": "path" if credentials.path_style else "virtual"},
            max_pool_connections=max_pool_connections,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            # The engine implements its own classified retry ladder, so botocore's
            # blind retries would only muddy error attribution and timing.
            retries={"max_attempts": 1, "mode": "standard"},
        )
        self._session = aioboto3.Session()
        self._stack: contextlib.AsyncExitStack | None = None
        self._client: Any = None

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._stack = contextlib.AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            self._session.client(
                "s3",
                endpoint_url=self.creds.endpoint_url,
                aws_access_key_id=self.creds.access_key,
                aws_secret_access_key=self.creds.secret_key,
                region_name=self.creds.region,
                config=self._boto_config,
                verify=self.creds.verify_tls,
            )
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise TransferError(
                ErrorClass.CONFIG_ERROR, "S3Client used outside its async context manager"
            )
        return self._client

    @property
    def bucket(self) -> str:
        if not self.creds.bucket:
            raise TransferError(ErrorClass.CONFIG_ERROR, "no bucket configured for this client")
        return self.creds.bucket

    # -- reading ------------------------------------------------------------

    async def list_prefix(
        self, prefix: str, *, page_size: int = 1000, start_after: str | None = None
    ) -> AsyncIterator[ObjectRef]:
        """Paginate ``ListObjectsV2``; resumable via ``start_after``."""
        kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Prefix": prefix,
            "MaxKeys": page_size,
        }
        if start_after:
            kwargs["StartAfter"] = start_after
        token: str | None = None
        try:
            while True:
                if token:
                    kwargs["ContinuationToken"] = token
                resp = await self.client.list_objects_v2(**kwargs)
                for item in resp.get("Contents", ()):
                    yield ObjectRef(
                        key=item["Key"],
                        size=item["Size"],
                        etag=_clean_etag(item.get("ETag", "")),
                        last_modified=item["LastModified"],
                        storage_class=item.get("StorageClass"),
                    )
                if not resp.get("IsTruncated"):
                    return
                token = resp.get("NextContinuationToken")
                if not token:
                    return
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def list_common_prefixes(self, prefix: str, delimiter: str = "/") -> list[str]:
        """List immediate child prefixes -- used to discover which years/months exist."""
        out: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {
                    "Bucket": self.bucket,
                    "Prefix": prefix,
                    "Delimiter": delimiter,
                }
                if token:
                    kwargs["ContinuationToken"] = token
                resp = await self.client.list_objects_v2(**kwargs)
                out.extend(p["Prefix"] for p in resp.get("CommonPrefixes", ()))
                if not resp.get("IsTruncated"):
                    return out
                token = resp.get("NextContinuationToken")
                if not token:
                    return out
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def head(self, key: str) -> ObjectMeta:
        try:
            resp = await self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise classify_exception(exc) from exc
        return ObjectMeta(
            key=key,
            size=resp["ContentLength"],
            etag=_clean_etag(resp.get("ETag", "")),
            last_modified=resp["LastModified"],
            content_type=resp.get("ContentType"),
            checksum_sha256=(resp.get("Metadata") or {}).get("c2w-sha256"),
            metadata=resp.get("Metadata") or {},
        )

    async def open_stream(self, key: str, *, offset: int = 0) -> AsyncIterator[bytes]:
        """Stream object bytes, optionally from ``offset`` via a Range request."""
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if offset > 0:
            kwargs["Range"] = f"bytes={offset}-"
        try:
            resp = await self.client.get_object(**kwargs)
        except Exception as exc:
            raise classify_exception(exc) from exc
        body = resp["Body"]
        try:
            while True:
                chunk = await body.read(_DEFAULT_CHUNK)
                if not chunk:
                    break
                yield chunk
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            with contextlib.suppress(Exception):
                body.close()

    # -- writing ------------------------------------------------------------

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        digest = hashlib.sha256(data).hexdigest()
        meta = dict(metadata or {})
        meta["c2w-sha256"] = digest
        try:
            resp = await self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                Metadata=meta,
                **({"ContentType": content_type} if content_type else {}),
            )
        except Exception as exc:
            raise classify_exception(exc) from exc
        return UploadResult(
            key=key,
            size=len(data),
            etag=_clean_etag(resp.get("ETag", "")),
            checksum_sha256=digest,
            multipart=False,
        )

    async def put_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size_hint: int | None = None,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """Write a stream, choosing single-shot or multipart by ``size_hint``.

        The SHA-256 is computed over the bytes actually written, so it is
        trustworthy even when the source lied about its size.
        """
        if size_hint is not None and size_hint < self.multipart_threshold:
            digest = hashlib.sha256()
            body = bytearray()
            async for chunk in hashing_stream(stream, digest, self.limiter):
                body.extend(chunk)
            return await self._put_small(key, bytes(body), digest, content_type, metadata)
        return await self._put_multipart(key, stream, size_hint, content_type, metadata)

    async def _put_small(
        self,
        key: str,
        data: bytes,
        digest: hashlib._Hash,
        content_type: str | None,
        metadata: dict[str, str] | None,
    ) -> UploadResult:
        hexdigest = digest.hexdigest()
        meta = dict(metadata or {})
        meta["c2w-sha256"] = hexdigest
        try:
            resp = await self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                Metadata=meta,
                ChecksumSHA256=base64.b64encode(digest.digest()).decode(),
                **({"ContentType": content_type} if content_type else {}),
            )
        except Exception as exc:
            raise classify_exception(exc) from exc
        return UploadResult(
            key=key,
            size=len(data),
            etag=_clean_etag(resp.get("ETag", "")),
            checksum_sha256=hexdigest,
            multipart=False,
        )

    async def _put_multipart(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        size_hint: int | None,
        content_type: str | None,
        metadata: dict[str, str] | None,
    ) -> UploadResult:
        part_size = self._choose_part_size(size_hint)
        digest = hashlib.sha256()
        meta = dict(metadata or {})
        upload_id: str | None = None
        parts: list[dict[str, Any]] = []
        total = 0
        try:
            created = await self.client.create_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                Metadata=meta,
                **({"ContentType": content_type} if content_type else {}),
            )
            upload_id = created["UploadId"]
            buffer = _PartBuffer(target=part_size, buf=bytearray())

            async for chunk in hashing_stream(stream, digest, self.limiter):
                for part in buffer.add(chunk):
                    total += len(part)
                    parts.append(await self._upload_part(key, upload_id, len(parts) + 1, part))
            tail = buffer.flush()
            if tail or not parts:
                total += len(tail)
                parts.append(await self._upload_part(key, upload_id, len(parts) + 1, tail))

            resp = await self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception as exc:
            if upload_id:
                # Abandoned multipart uploads keep consuming storage and appear
                # on the Wasabi bill until aborted, so never leave one behind.
                with contextlib.suppress(Exception):
                    await self.client.abort_multipart_upload(
                        Bucket=self.bucket, Key=key, UploadId=upload_id
                    )
            raise classify_exception(exc) from exc

        hexdigest = digest.hexdigest()
        with contextlib.suppress(Exception):
            # Record the whole-object digest as user metadata; a multipart ETag
            # is a hash of part hashes and cannot be compared to it directly.
            await self.client.copy_object(
                Bucket=self.bucket,
                Key=key,
                CopySource={"Bucket": self.bucket, "Key": key},
                Metadata={**meta, "c2w-sha256": hexdigest},
                MetadataDirective="REPLACE",
                **({"ContentType": content_type} if content_type else {}),
            )
        return UploadResult(
            key=key,
            size=total,
            etag=_clean_etag(resp.get("ETag", "")),
            checksum_sha256=hexdigest,
            multipart=True,
            parts=len(parts),
        )

    async def _upload_part(
        self, key: str, upload_id: str, number: int, body: bytes
    ) -> dict[str, Any]:
        resp = await self.client.upload_part(
            Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body
        )
        return {"ETag": resp["ETag"], "PartNumber": number}

    def _choose_part_size(self, size_hint: int | None) -> int:
        """Grow the part size when needed to stay under the 10,000-part cap."""
        part = self.multipart_chunk
        if size_hint and size_hint / part > _MAX_PARTS:
            needed = -(-size_hint // _MAX_PARTS)
            part = max(part, needed)
        return max(part, _MIN_PART_SIZE)

    # -- misc ---------------------------------------------------------------

    async def presign_get(
        self, key: str, *, ttl_seconds: int = 300, download_filename: str | None = None
    ) -> str:
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if download_filename:
            safe = download_filename.replace('"', "")
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        try:
            return await self.client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=ttl_seconds
            )
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def delete(self, key: str) -> None:
        try:
            await self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise classify_exception(exc) from exc

    async def probe(self) -> None:
        """Cheapest call that proves credentials, ACL and bucket are all good."""
        try:
            await self.client.list_objects_v2(Bucket=self.bucket, MaxKeys=1)
        except Exception as exc:
            raise classify_exception(exc) from exc


def _clean_etag(etag: str) -> str:
    return etag.strip('"')
