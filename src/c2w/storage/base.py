"""Storage abstractions.

The core of the platform talks to :class:`ObjectSource` and
:class:`ObjectDestination`, never to "CommPeak" or "Wasabi" directly.  That is a
deliberate constraint: CommPeak is simply the first S3-compatible source and
Wasabi the first S3-compatible destination.  Adding a second PBX vendor, or
moving a brand to MinIO/Backblaze, must not require touching the transfer
engine, the inventory scanner or the media gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "ObjectDestination",
    "ObjectMeta",
    "ObjectRef",
    "ObjectSource",
    "S3Credentials",
    "UploadResult",
]


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """One object as returned by a listing."""

    key: str
    size: int
    etag: str
    last_modified: datetime
    storage_class: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectMeta:
    """Result of a HEAD request."""

    key: str
    size: int
    etag: str
    last_modified: datetime
    content_type: str | None = None
    checksum_sha256: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class UploadResult:
    """Outcome of writing one object to a destination."""

    key: str
    size: int
    etag: str
    #: Hex SHA-256 computed over the bytes as they streamed through.  This is
    #: what makes verification independent of ETag semantics, which differ
    #: between single-part and multipart uploads.
    checksum_sha256: str
    multipart: bool
    parts: int = 1


@dataclass(frozen=True, slots=True)
class S3Credentials:
    """Everything needed to reach one S3-compatible endpoint.

    ``path_style`` defaults to True because CommPeak requires it and Wasabi
    accepts it, so the safe default works for both.
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    bucket: str = ""
    path_style: bool = True
    signature_version: str = "s3v4"
    verify_tls: bool = True


@runtime_checkable
class ObjectSource(Protocol):
    """Read-only access to a recordings bucket."""

    async def list_prefix(
        self, prefix: str, *, page_size: int = 1000, start_after: str | None = None
    ) -> AsyncIterator[ObjectRef]:
        """Yield objects under ``prefix``, ascending by key.

        ``start_after`` makes a long listing resumable after a crash without
        re-reading pages we already persisted.
        """
        ...

    async def head(self, key: str) -> ObjectMeta: ...

    def open_stream(self, key: str, *, offset: int = 0) -> AsyncIterator[bytes]:
        """Stream an object's bytes, optionally resuming from ``offset``."""
        ...

    async def delete(self, key: str) -> None:
        """Delete an object. Irreversible on CommPeak -- callers must gate this."""
        ...

    async def probe(self) -> None:
        """Raise :class:`~c2w.storage.errors.TransferError` if unusable."""
        ...


@runtime_checkable
class ObjectDestination(Protocol):
    """Write access to an archive bucket."""

    async def put_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size_hint: int | None = None,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult: ...

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult: ...

    async def head(self, key: str) -> ObjectMeta: ...

    async def presign_get(
        self, key: str, *, ttl_seconds: int = 300, download_filename: str | None = None
    ) -> str: ...

    async def list_prefix(
        self, prefix: str, *, page_size: int = 1000, start_after: str | None = None
    ) -> AsyncIterator[ObjectRef]: ...

    async def delete(self, key: str) -> None: ...

    async def probe(self) -> None: ...


def sort_keys(refs: Sequence[ObjectRef]) -> list[ObjectRef]:
    """Stable ordering helper used by reconciliation diffs."""
    return sorted(refs, key=lambda r: r.key)
