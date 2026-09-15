"""The only thing in this codebase that can delete from CommPeak.

Kept apart from `CommPeakSource` on purpose, and the separation is the safety
property rather than a tidiness one:

* `CommPeakSource` raises `SourceIsReadOnly` from every mutating method. It
  cannot delete. That does not change, and no setting reaches it.
* `CommPeakDeleter` can delete and can do nothing else -- it has no listing,
  no reading, no copying. It exists only when a caller has gone and fetched
  credentials that are stored in their own columns and ship empty.

So "deletion is off" is not a flag somebody can flip by accident. With no
delete credentials configured there is no object capable of deleting, whatever
any setting says. Arming day D means an operator issuing a second credential
pair at CommPeak with delete permission and entering it deliberately; the
read-only pair the rest of the system runs on never gains that permission.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any, Self

import aioboto3
from botocore.config import Config

from c2w.logging import get_logger
from c2w.storage.base import S3Credentials
from c2w.storage.errors import ErrorClass, TransferError, classify_exception

__all__ = ["CommPeakDeleter", "DeleteOutcome"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeleteOutcome:
    key: str
    deleted: bool
    detail: str = ""


class CommPeakDeleter:
    """Delete one object at a time from a CommPeak bucket.

    One at a time, deliberately. `DeleteObjects` can remove a thousand keys in
    a request and is the obvious thing to reach for, but it turns a single
    mistaken call into a thousand losses and reports partial failure in a way
    that is easy to skim past. At the rates CommPeak tolerates -- it has
    blocked this host three times in a week over request rate -- batching is
    not the constraint anyway.
    """

    def __init__(self, creds: S3Credentials) -> None:
        if not creds.access_key or not creds.secret_key:
            raise ValueError(
                "refusing to build a deleter without delete credentials: "
                "deletion is armed by entering a separate credential pair, "
                "not by a setting"
            )
        self.creds = creds
        self._session = aioboto3.Session()
        self._client: Any = None
        self._stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        self._stack = contextlib.AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            self._session.client(
                "s3",
                endpoint_url=self.creds.endpoint_url,
                aws_access_key_id=self.creds.access_key,
                aws_secret_access_key=self.creds.secret_key,
                region_name=self.creds.region,
                config=Config(
                    s3={"addressing_style": "path"},
                    signature_version="s3v4",
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    async def delete(self, key: str) -> DeleteOutcome:
        """Remove one object. Never retried here.

        A delete that fails is left alone rather than attempted again: the
        failures worth worrying about are refusals, and retrying into a refusal
        is what has caused every outage on this source. The caller decides
        whether to stop, and after a refusal it should.
        """
        if self._client is None:
            raise TransferError(
                ErrorClass.UNKNOWN,
                "the deleter was used outside its context manager -- a fault in "
                "c2w, not in this account's configuration",
            )
        try:
            await self._client.delete_object(Bucket=self.creds.bucket, Key=key)
        except Exception as exc:
            error = exc if isinstance(exc, TransferError) else classify_exception(exc)
            log.warning(
                "source.delete_failed",
                bucket=self.creds.bucket,
                key=key,
                error_class=str(getattr(error, "error_class", "UNKNOWN")),
            )
            return DeleteOutcome(key=key, deleted=False, detail=str(error)[:300])
        log.info("source.deleted", bucket=self.creds.bucket, key=key)
        return DeleteOutcome(key=key, deleted=True)
