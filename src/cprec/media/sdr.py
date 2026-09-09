"""Media gateway -- the recording delivery layer.

Playback authorises the request, then hands the browser a short-lived presigned
URL to the archive.  The browser never receives storage credentials, and the
audio bytes do not pass through the application, which matters when a single
brand holds terabytes of recordings and several supervisors are reviewing calls
at once.

Proxy mode exists for the case where a presigned URL is unacceptable (a customer
policy that forbids direct object-store access, say).  It costs application
bandwidth, so it is opt-in and never the default.

Every play, download and refusal is audited.  For call recordings, who listened
to what is itself sensitive information and is routinely the subject of
compliance review.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cprec.auth.rbac import Permission, has_permission
from cprec.crypto import open_sealed
from cprec.db.base import RecordingState
from cprec.db.models.auth import User
from cprec.db.models.core import AuditEvent, Brand, Recording, StorageDestination
from cprec.logging import get_logger
from cprec.settings import settings_service
from cprec.storage.wasabi import WasabiDestination, wasabi_credentials

log = get_logger(__name__)

__all__ = ["MediaAccess", "MediaDenied", "audit_media_access", "authorise", "playback_url"]


class MediaAction(enum.StrEnum):
    PLAY = "PLAY"
    DOWNLOAD = "DOWNLOAD"


class MediaDenied(PermissionError):
    """Access to a recording was refused.

    Carries a machine-readable reason so the audit row records *why*, which is
    what makes an access log useful during a review.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(slots=True)
class MediaAccess:
    recording: Recording
    action: MediaAction
    url: str | None = None
    expires_at: datetime | None = None
    proxied: bool = False
    filename: str | None = None


def _filename_for(recording: Recording) -> str:
    """A download name that means something to a human.

    The source key's basename is a PBX-generated string; a name built from the
    call's own details is what someone filing an export actually wants.
    """
    stamp = (recording.started_at or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    parts = [stamp]
    if recording.direction:
        parts.append(recording.direction)
    if recording.number:
        parts.append(recording.number)
    if recording.seq:
        parts.append(f"part{recording.seq + 1}")
    ext = recording.file_ext or "flac"
    return "-".join(parts) + f".{ext}"


async def authorise(
    session: AsyncSession,
    user: User,
    recording: Recording,
    action: MediaAction,
) -> None:
    """Check that this user may take this action on this recording.

    Play and download are separate permissions on purpose: some organisations
    allow staff to listen for quality review but forbid taking a copy off the
    platform.
    """
    needed = (
        Permission.RECORDINGS_PLAY if action is MediaAction.PLAY else Permission.RECORDINGS_DOWNLOAD
    )
    if not has_permission(user.role, needed):
        raise MediaDenied("permission_denied", f"your role may not {action.lower()} recordings")

    # Defence in depth. RLS already scopes the query that loaded this recording,
    # but an explicit check means a future code path that bypasses the scoped
    # session cannot leak across brands.
    if not user.is_super_admin and recording.brand_id != user.brand_id:
        raise MediaDenied("wrong_brand", "recording belongs to another organisation")

    if not recording.state.playable:
        if recording.state in (RecordingState.FAILED, RecordingState.MISSING_SOURCE):
            raise MediaDenied("unavailable", "this recording is not available in the archive")
        raise MediaDenied("still_syncing", "this recording is still being archived")


async def playback_url(
    session: AsyncSession,
    user: User,
    recording: Recording,
    *,
    action: MediaAction = MediaAction.PLAY,
    ip: str | None = None,
    user_agent: str | None = None,
) -> MediaAccess:
    """Authorise and produce a playable URL, auditing the outcome either way."""
    try:
        await authorise(session, user, recording, action)
    except MediaDenied as denied:
        await audit_media_access(
            session,
            user,
            recording,
            action,
            result="DENIED",
            ip=ip,
            user_agent=user_agent,
            detail={"reason": denied.reason},
        )
        raise

    # Denials after the permission check are audited too. An access log with
    # gaps is a weak access log: during a review, "no record" must mean "did not
    # happen", not "happened but failed a later check".
    async def _deny(reason: str, message: str) -> MediaDenied:
        denied = MediaDenied(reason, message)
        await audit_media_access(
            session,
            user,
            recording,
            action,
            result="DENIED",
            ip=ip,
            user_agent=user_agent,
            detail={"reason": reason},
        )
        return denied

    if recording.destination_id is None or not recording.destination_key:
        raise await _deny("no_archive_copy", "this recording has no archive copy yet")

    destination = (
        await session.execute(
            select(StorageDestination).where(StorageDestination.id == recording.destination_id)
        )
    ).scalar_one_or_none()
    if destination is None:
        raise await _deny("no_destination", "the archive for this recording is not configured")

    brand = (
        await session.execute(select(Brand).where(Brand.id == recording.brand_id))
    ).scalar_one()

    ttl = await settings_service.get_int(session, "media.presign_ttl_seconds")
    filename = _filename_for(recording)

    creds = wasabi_credentials(
        bucket=destination.bucket,
        access_key=open_sealed(
            destination.access_key_sealed,
            key_id=brand.encryption_key_id or "",
            wrapped_key=brand.encryption_key_wrapped or "",
            aad=f"destination:{destination.id}:access_key",
        ),
        secret_key=open_sealed(
            destination.secret_sealed,
            key_id=brand.encryption_key_id or "",
            wrapped_key=brand.encryption_key_wrapped or "",
            aad=f"destination:{destination.id}:secret_key",
        ),
        region=destination.region,
        endpoint_url=destination.endpoint,
    )

    async with WasabiDestination(creds) as dest:
        url = await dest.presign_get(
            recording.destination_key,
            ttl_seconds=ttl,
            download_filename=filename if action is MediaAction.DOWNLOAD else None,
        )

    await audit_media_access(
        session,
        user,
        recording,
        action,
        result="SUCCESS",
        ip=ip,
        user_agent=user_agent,
        detail={"destination": destination.name, "ttl_seconds": ttl},
    )
    return MediaAccess(
        recording=recording,
        action=action,
        url=url,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        filename=filename,
    )


async def audit_media_access(
    session: AsyncSession,
    user: User,
    recording: Recording,
    action: MediaAction,
    *,
    result: str,
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write the append-only audit row for a media access attempt."""
    session.add(
        AuditEvent(
            brand_id=recording.brand_id,
            actor_user_id=user.id,
            actor_label=user.email,
            ip=ip,
            user_agent=user_agent,
            action=str(action),
            result=result,
            recording_id=recording.id,
            call_uuid=recording.call_uuid,
            detail=detail or {},
        )
    )
    await session.flush()
