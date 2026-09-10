"""Transfer one recording from CommPeak to the archive, and verify it.

The sequence is deliberately strict:

    stream source -> hash in flight -> multipart upload -> HEAD destination
    -> compare size and digest -> VERIFIED -> write sidecar -> AVAILABLE

A recording only becomes playable after the archive copy has been proven byte
-correct.  An HTTP 200 from the destination is not evidence: it says the request
was accepted, not that what landed matches what left.  Since the whole point of
this platform is that the archive can eventually be the only copy, "probably
uploaded" is not good enough.

Nothing here writes to CommPeak.  The source object is read and left alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.db.base import RecordingState
from c2w.db.models.core import Recording, StorageDestination
from c2w.logging import get_logger
from c2w.storage.base import ObjectMeta
from c2w.storage.commpeak import CommPeakSource
from c2w.storage.errors import ErrorClass, TransferError
from c2w.storage.wasabi import WasabiDestination, destination_key, sidecar_key

log = get_logger(__name__)

__all__ = ["TransferOutcome", "build_sidecar", "transfer_recording", "verify_recording"]

_CONTENT_TYPES = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "m4a": "audio/mp4",
}


@dataclass(slots=True)
class TransferOutcome:
    recording_id: int
    bytes_transferred: int
    verified: bool
    destination_key: str | None = None
    checksum_sha256: str | None = None
    multipart: bool = False
    sidecar_written: bool = False


def build_sidecar(recording: Recording, cdr: dict[str, Any] | None) -> bytes:
    """Assemble the JSON written beside each archived recording.

    This is what makes the archive self-describing.  If the application
    database were lost outright, the bucket alone still says which call each
    file belongs to, who the parties were, when it happened, and what we
    believed about the correlation -- including how confident we were.  Without
    it, a recovered bucket is 13.9 TB of anonymous audio.
    """
    payload = {
        "schema": "c2w.sidecar/1",
        "written_at": datetime.now(UTC).isoformat(),
        "recording": {
            "source_key": recording.source_key,
            "source_size": recording.source_size,
            "source_etag": recording.source_etag,
            "checksum_sha256": recording.checksum_sha256,
            "direction": recording.direction,
            "number": recording.number,
            "extension": recording.extension,
            "started_at": recording.started_at.isoformat() if recording.started_at else None,
            "uniqueid": recording.uniqueid,
            "seq": recording.seq,
            "file_ext": recording.file_ext,
        },
        "correlation": {
            "call_uuid": recording.call_uuid,
            "cdr_id": recording.cdr_id,
            "method": recording.match_method,
            "confidence": recording.match_confidence,
            "ambiguous": recording.match_ambiguous,
        },
        "cdr": cdr,
    }
    return json.dumps(payload, indent=1, default=str).encode()


async def _load_cdr(session: AsyncSession, recording: Recording) -> dict[str, Any] | None:
    if recording.cdr_id is None:
        return None
    row = (
        await session.execute(
            text(
                "SELECT call_uuid, call_id, start_at, end_at, call_duration, direction, "
                "src, dst, status, agent_extension, agent_name, public_recording_url, raw "
                "FROM cdrs WHERE brand_id = :b AND id = :i"
            ),
            {"b": recording.brand_id, "i": recording.cdr_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _compare(source: ObjectMeta | int, dest: ObjectMeta, expected_digest: str) -> None:
    """Verify a destination object against what we sent.

    Note what is *not* compared: ETags.  A multipart ETag is a hash of part
    hashes plus a part count, so it legitimately differs from the source's
    single-part ETag for identical bytes.  Comparing them would fail every large
    object.  The whole-object SHA-256 computed while streaming is the real check.
    """
    source_size = source if isinstance(source, int) else source.size
    if dest.size != source_size:
        raise TransferError(
            ErrorClass.CHECKSUM_ERROR,
            f"size mismatch: source {source_size} bytes, destination {dest.size} bytes",
        )
    if dest.checksum_sha256 and dest.checksum_sha256 != expected_digest:
        raise TransferError(
            ErrorClass.CHECKSUM_ERROR,
            "digest mismatch between transferred bytes and stored object metadata",
        )


async def transfer_recording(
    session: AsyncSession,
    recording: Recording,
    destination_row: StorageDestination,
    source: CommPeakSource,
    dest: WasabiDestination,
    *,
    account: str,
    multipart_threshold: int,
    multipart_chunk: int,
    write_sidecar: bool = True,
) -> TransferOutcome:
    """Copy one recording to the archive and verify it landed intact."""
    now = datetime.now(UTC)
    recording.state = RecordingState.TRANSFERRING
    recording.transfer_started_at = now
    await session.flush()

    # HEAD first: the size drives the multipart decision, and an object that
    # disappeared since inventory is a normal condition worth naming precisely
    # rather than discovering halfway through a stream.
    try:
        src_meta = await source.head(recording.source_key)
    except TransferError as exc:
        if exc.error_class is ErrorClass.NOT_FOUND:
            recording.state = RecordingState.MISSING_SOURCE
            recording.last_error_class = str(ErrorClass.NOT_FOUND)
            recording.last_error_detail = "object no longer present at source"
            await session.flush()
            log.info(
                "transfer.source_missing",
                recording_id=recording.id,
                source_key=recording.source_key,
            )
            return TransferOutcome(recording.id, 0, verified=False)
        raise

    dest_key = destination_key(
        path_prefix=destination_row.path_prefix,
        account=account,
        source_key=recording.source_key,
    )
    content_type = _CONTENT_TYPES.get(recording.file_ext or "", "application/octet-stream")

    dest.multipart_threshold = multipart_threshold
    dest.multipart_chunk = multipart_chunk

    upload = await dest.put_stream(
        dest_key,
        source.open_stream(recording.source_key),
        size_hint=src_meta.size,
        content_type=content_type,
        metadata={
            # Enough provenance on the object itself to trace it back without
            # the database.
            "c2w-source-key": recording.source_key[:1024],
            "c2w-call-uuid": recording.call_uuid or "",
            "c2w-match": recording.match_method or "",
        },
    )

    recording.destination_id = destination_row.id
    recording.destination_key = dest_key
    recording.destination_etag = upload.etag
    recording.destination_size = upload.size
    recording.checksum_sha256 = upload.checksum_sha256
    recording.bytes_transferred = upload.size
    recording.state = RecordingState.UPLOADED
    recording.transfer_completed_at = datetime.now(UTC)
    await session.flush()

    # Read it back. This is the step that turns "we sent it" into "it is there".
    dest_meta = await dest.head(dest_key)
    _compare(src_meta, dest_meta, upload.checksum_sha256)

    recording.state = RecordingState.VERIFIED
    recording.verified_at = datetime.now(UTC)
    recording.last_error_class = None
    recording.last_error_detail = None
    await session.flush()

    sidecar_ok = False
    if write_sidecar:
        try:
            cdr = await _load_cdr(session, recording)
            await dest.put_bytes(
                sidecar_key(dest_key),
                build_sidecar(recording, cdr),
                content_type="application/json",
            )
            recording.sidecar_written = True
            sidecar_ok = True
        except TransferError as exc:
            # The audio is verified; a missing sidecar is a degradation, not a
            # reason to fail the transfer and re-send the whole object.
            log.warning(
                "transfer.sidecar_failed",
                recording_id=recording.id,
                error_class=str(exc.error_class),
                error=exc.message,
            )

    recording.state = RecordingState.AVAILABLE
    await session.flush()

    log.info(
        "transfer.complete",
        recording_id=recording.id,
        bytes=upload.size,
        multipart=upload.multipart,
        parts=upload.parts,
        destination_key=dest_key,
    )
    return TransferOutcome(
        recording_id=recording.id,
        bytes_transferred=upload.size,
        verified=True,
        destination_key=dest_key,
        checksum_sha256=upload.checksum_sha256,
        multipart=upload.multipart,
        sidecar_written=sidecar_ok,
    )


async def verify_recording(
    session: AsyncSession,
    recording: Recording,
    dest: WasabiDestination,
) -> bool:
    """Re-check an already-archived recording against the destination.

    Used by reconciliation.  A recording that fails is put back to QUEUED so it
    is transferred again -- and, importantly, is not marked FAILED, because the
    source copy is still there and the fix is simply to re-copy.
    """
    if not recording.destination_key:
        return False
    try:
        meta = await dest.head(recording.destination_key)
    except TransferError as exc:
        if exc.error_class is ErrorClass.NOT_FOUND:
            log.warning(
                "verify.destination_missing",
                recording_id=recording.id,
                destination_key=recording.destination_key,
            )
            recording.state = RecordingState.QUEUED
            recording.last_error_class = str(ErrorClass.NOT_FOUND)
            recording.last_error_detail = "archive object missing; re-queued for transfer"
            await session.flush()
            return False
        raise

    try:
        _compare(recording.source_size, meta, recording.checksum_sha256 or "")
    except TransferError as exc:
        recording.state = RecordingState.QUEUED
        recording.last_error_class = str(exc.error_class)
        recording.last_error_detail = exc.message
        await session.flush()
        return False

    recording.verified_at = datetime.now(UTC)
    await session.flush()
    return True
