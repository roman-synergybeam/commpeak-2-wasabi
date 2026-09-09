"""Build storage clients from database rows.

The only place credentials are unsealed.  Keeping it in one function means
there is a single point to audit for credential handling, and callers work with
clients rather than secrets -- nothing else in the codebase needs to know that
credentials are encrypted at all.

Unsealed values live only as local variables for the lifetime of the call and
are never logged, cached, or attached to a model.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cprec.crypto import CryptoError, open_sealed
from cprec.db.models.core import Brand, CommPeakConnection, StorageDestination
from cprec.storage.commpeak import CommPeakSource, commpeak_credentials
from cprec.storage.errors import ErrorClass, TransferError
from cprec.storage.s3_adapter import RateLimiter
from cprec.storage.wasabi import WasabiDestination, wasabi_credentials

__all__ = ["CredentialsUnavailable", "open_destination", "open_source"]


class CredentialsUnavailable(TransferError):
    """Stored credentials could not be unsealed.

    Almost always means the master key changed or was restored from a different
    backup than the database.  Worth distinguishing from a wrong password: the
    credentials in the database are intact, they simply cannot be opened.
    """

    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorClass.CONFIG_ERROR,
            message,
            hint=(
                "the master key does not match the one these credentials were sealed with; "
                "restore /etc/cprec/master.key or re-enter the credentials"
            ),
        )


async def _brand_keys(session: AsyncSession, brand_id: int) -> tuple[str, str]:
    brand = (await session.execute(select(Brand).where(Brand.id == brand_id))).scalar_one_or_none()
    if brand is None or not brand.encryption_key_id or not brand.encryption_key_wrapped:
        raise CredentialsUnavailable(f"brand {brand_id} has no encryption key configured")
    return brand.encryption_key_id, brand.encryption_key_wrapped


def _unseal(value: str, *, key_id: str, wrapped: str, aad: str, label: str) -> str:
    try:
        return open_sealed(value, key_id=key_id, wrapped_key=wrapped, aad=aad)
    except CryptoError as exc:
        raise CredentialsUnavailable(f"could not unseal {label}: {exc}") from exc


async def open_source(
    session: AsyncSession,
    connection: CommPeakConnection,
    *,
    limiter: RateLimiter | None = None,
) -> CommPeakSource:
    """Open a read-only client for a CommPeak bucket.

    Returns an unentered context manager; the caller uses ``async with``.
    """
    key_id, wrapped = await _brand_keys(session, connection.brand_id)
    creds = commpeak_credentials(
        bucket=connection.s3_bucket,
        access_key=_unseal(
            connection.s3_access_key_sealed,
            key_id=key_id,
            wrapped=wrapped,
            aad=f"connection:{connection.id}:access_key",
            label=f"connection {connection.id} access key",
        ),
        secret_key=_unseal(
            connection.s3_secret_sealed,
            key_id=key_id,
            wrapped=wrapped,
            aad=f"connection:{connection.id}:secret_key",
            label=f"connection {connection.id} secret",
        ),
        endpoint_url=connection.s3_endpoint,
        region=connection.s3_region,
    )
    return CommPeakSource(creds, limiter=limiter)


async def open_destination(
    session: AsyncSession,
    destination: StorageDestination,
    *,
    limiter: RateLimiter | None = None,
) -> WasabiDestination:
    """Open a client for an archive destination."""
    key_id, wrapped = await _brand_keys(session, destination.brand_id)
    creds = wasabi_credentials(
        bucket=destination.bucket,
        access_key=_unseal(
            destination.access_key_sealed,
            key_id=key_id,
            wrapped=wrapped,
            aad=f"destination:{destination.id}:access_key",
            label=f"destination {destination.id} access key",
        ),
        secret_key=_unseal(
            destination.secret_sealed,
            key_id=key_id,
            wrapped=wrapped,
            aad=f"destination:{destination.id}:secret_key",
            label=f"destination {destination.id} secret",
        ),
        region=destination.region,
        endpoint_url=destination.endpoint,
    )
    client = WasabiDestination(creds, limiter=limiter)
    return client
