"""Wasabi (and any S3-compatible archive) as an :class:`ObjectDestination`.

Wasabi supports the standard S3 API and SDKs, so this is a thin configuration
of :class:`~c2w.storage.s3_adapter.S3Client` plus the destination key layout
and its sidecar-metadata convention.
"""

from __future__ import annotations

from c2w.storage.base import S3Credentials
from c2w.storage.errors import ErrorClass, TransferError
from c2w.storage.s3_adapter import S3Client

__all__ = [
    "WASABI_REGIONS",
    "WasabiDestination",
    "destination_key",
    "sidecar_key",
    "wasabi_credentials",
]

#: Wasabi's published regional endpoints.  Kept as data so a new region needs a
#: one-line change rather than a code change.
WASABI_REGIONS: dict[str, str] = {
    "us-east-1": "https://s3.us-east-1.wasabisys.com",
    "us-east-2": "https://s3.us-east-2.wasabisys.com",
    "us-central-1": "https://s3.us-central-1.wasabisys.com",
    "us-west-1": "https://s3.us-west-1.wasabisys.com",
    "ca-central-1": "https://s3.ca-central-1.wasabisys.com",
    "eu-central-1": "https://s3.eu-central-1.wasabisys.com",
    "eu-central-2": "https://s3.eu-central-2.wasabisys.com",
    "eu-west-1": "https://s3.eu-west-1.wasabisys.com",
    "eu-west-2": "https://s3.eu-west-2.wasabisys.com",
    "eu-south-1": "https://s3.eu-south-1.wasabisys.com",
    "ap-northeast-1": "https://s3.ap-northeast-1.wasabisys.com",
    "ap-northeast-2": "https://s3.ap-northeast-2.wasabisys.com",
    "ap-southeast-1": "https://s3.ap-southeast-1.wasabisys.com",
    "ap-southeast-2": "https://s3.ap-southeast-2.wasabisys.com",
}


def wasabi_credentials(
    *,
    bucket: str,
    access_key: str,
    secret_key: str,
    region: str = "eu-central-1",
    endpoint_url: str | None = None,
) -> S3Credentials:
    """Build Wasabi credentials, resolving the endpoint from the region."""
    resolved = endpoint_url or WASABI_REGIONS.get(region)
    if not resolved:
        raise TransferError(
            ErrorClass.CONFIG_ERROR,
            f"unknown Wasabi region {region!r}; pass endpoint_url explicitly",
        )
    return S3Credentials(
        endpoint_url=resolved,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        bucket=bucket,
        path_style=True,
        signature_version="s3v4",
    )


class WasabiDestination(S3Client):
    """Archive destination.  Wasabi is the source of truth for retained media."""


def destination_key(
    *,
    path_prefix: str,
    brand_slug: str,
    tenant_slug: str,
    source_key: str,
) -> str:
    """Lay out the archive key.

    Brand and tenant lead the path so a brand's objects are contiguous -- that
    makes per-brand lifecycle rules, usage accounting and (if a brand ever
    leaves) bulk deletion straightforward.  The source's date hierarchy and
    filename are preserved verbatim underneath, so an archived object can always
    be traced back to its origin without consulting the database.
    """
    tail = source_key.lstrip("/")
    parts = [p for p in (path_prefix.strip("/"), brand_slug, tenant_slug) if p]
    return "/".join(parts) + "/" + tail


def sidecar_key(dest_key: str) -> str:
    """Key of the JSON metadata written beside a recording.

    The sidecar makes the archive self-describing: if the application database
    were lost entirely, the bucket alone still says which call each file belongs
    to, who the parties were and when it happened.
    """
    return f"{dest_key}.cdr.json"
