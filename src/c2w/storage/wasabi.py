"""Wasabi (and any S3-compatible archive) as an :class:`ObjectDestination`.

Wasabi supports the standard S3 API and SDKs, so this is a thin configuration
of :class:`~c2w.storage.s3_adapter.S3Client` plus the destination key layout
and its sidecar-metadata convention.
"""

from __future__ import annotations

import re
from typing import Final

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


#: Characters allowed in the account folder. Dots are kept on purpose -- the
#: folder is meant to read as `go4rex.pbx`, matching the CommPeak account it
#: came from, so somebody browsing the bucket recognises it immediately.
_UNSAFE_IN_KEY: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


def account_folder(name: str) -> str:
    """The archive folder for one CommPeak account.

    The account *name* rather than the tenant slug, because the name is what
    the account is called at CommPeak and the slug is not: slugs collide and
    get a counter appended, so `go4rex.pbx` had the tenant slug `go4rex-2` and
    would have archived into a folder nobody could identify.

    Sanitised because the name is operator-entered free text and this ends up
    in an object key: anything outside `[A-Za-z0-9._-]` becomes a hyphen, and
    leading dots and hyphens are trimmed so a name cannot produce `..` or a
    hidden-looking path segment.
    """
    cleaned = _UNSAFE_IN_KEY.sub("-", name.strip()).strip(".-")
    return cleaned or "unnamed-account"


def destination_key(
    *,
    path_prefix: str,
    account: str,
    source_key: str,
) -> str:
    """Lay out the archive key.

    ``{path_prefix}/{account}/{source key verbatim}``.

    The account folder leads, so the bucket's top level reads as the list of
    CommPeak accounts it holds -- `go4rex.pbx`, `go4rex.td`, `go4rexnew.td` and
    so on. That was asked for directly, and it is also the arrangement that
    makes the archive navigable without the database.

    The brand is deliberately *not* in the path any more. Each organisation has
    its own bucket, so a brand segment inside it was a level that told you
    nothing and pushed the account names one deeper than they should be. Brand
    isolation is the bucket and the credentials, not a folder name.

    The source key is preserved verbatim underneath -- including CommPeak's own
    `recordings/YYYY/MM/DD/` tree -- so an archived object can always be traced
    back to its origin without consulting anything.
    """
    tail = source_key.lstrip("/")
    parts = [p for p in (path_prefix.strip("/"), account_folder(account)) if p]
    return "/".join(parts) + "/" + tail


def sidecar_key(dest_key: str) -> str:
    """Key of the JSON metadata written beside a recording.

    The sidecar makes the archive self-describing: if the application database
    were lost entirely, the bucket alone still says which call each file belongs
    to, who the parties were and when it happened.
    """
    return f"{dest_key}.cdr.json"
