"""Error classification for S3 transfers.

Every failure is mapped to an :class:`ErrorClass` before it reaches the retry
logic, because the correct response differs sharply by cause: a missing IP
whitelist entry must fail fast and alert a human, while a 503 from Wasabi should
simply back off.  Retrying an ``AUTH_ERROR`` 5 times only delays the alert.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = ["ErrorClass", "TransferError", "classify_exception"]


class ErrorClass(enum.StrEnum):
    AUTH_ERROR = "AUTH_ERROR"
    ACL_ERROR = "ACL_ERROR"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMIT = "RATE_LIMIT"
    NETWORK_ERROR = "NETWORK_ERROR"
    S3_ERROR = "S3_ERROR"
    CHECKSUM_ERROR = "CHECKSUM_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    PERMISSION_ERROR = "PERMISSION_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    UNKNOWN = "UNKNOWN"

    @property
    def retryable(self) -> bool:
        return self in _RETRYABLE

    @property
    def alert_immediately(self) -> bool:
        """Classes that indicate a human must intervene, not a transient blip."""
        return self in _ALERTING


_RETRYABLE: Final[frozenset[ErrorClass]] = frozenset(
    {
        ErrorClass.RATE_LIMIT,
        ErrorClass.NETWORK_ERROR,
        ErrorClass.S3_ERROR,
        ErrorClass.STORAGE_ERROR,
        ErrorClass.CHECKSUM_ERROR,
        ErrorClass.UNKNOWN,
    }
)

_ALERTING: Final[frozenset[ErrorClass]] = frozenset(
    {
        ErrorClass.AUTH_ERROR,
        ErrorClass.ACL_ERROR,
        ErrorClass.PERMISSION_ERROR,
        ErrorClass.CONFIG_ERROR,
    }
)

# S3 / botocore error codes -> our classes.
_CODE_MAP: Final[dict[str, ErrorClass]] = {
    "InvalidAccessKeyId": ErrorClass.AUTH_ERROR,
    "SignatureDoesNotMatch": ErrorClass.AUTH_ERROR,
    "InvalidToken": ErrorClass.AUTH_ERROR,
    "ExpiredToken": ErrorClass.AUTH_ERROR,
    "TokenRefreshRequired": ErrorClass.AUTH_ERROR,
    "AccountProblem": ErrorClass.AUTH_ERROR,
    "AccessDenied": ErrorClass.ACL_ERROR,
    "AllAccessDisabled": ErrorClass.ACL_ERROR,
    "RequestTimeTooSkewed": ErrorClass.CONFIG_ERROR,
    "NoSuchBucket": ErrorClass.CONFIG_ERROR,
    "InvalidBucketName": ErrorClass.CONFIG_ERROR,
    "PermanentRedirect": ErrorClass.CONFIG_ERROR,
    "AuthorizationHeaderMalformed": ErrorClass.CONFIG_ERROR,
    "NoSuchKey": ErrorClass.NOT_FOUND,
    "NoSuchUpload": ErrorClass.NOT_FOUND,
    "404": ErrorClass.NOT_FOUND,
    "SlowDown": ErrorClass.RATE_LIMIT,
    "TooManyRequests": ErrorClass.RATE_LIMIT,
    "RequestLimitExceeded": ErrorClass.RATE_LIMIT,
    "503": ErrorClass.RATE_LIMIT,
    "RequestTimeout": ErrorClass.NETWORK_ERROR,
    "RequestTimeoutException": ErrorClass.NETWORK_ERROR,
    "ConnectionError": ErrorClass.NETWORK_ERROR,
    "InternalError": ErrorClass.S3_ERROR,
    "ServiceUnavailable": ErrorClass.S3_ERROR,
    "500": ErrorClass.S3_ERROR,
    "BadDigest": ErrorClass.CHECKSUM_ERROR,
    "InvalidDigest": ErrorClass.CHECKSUM_ERROR,
    "XAmzContentSHA256Mismatch": ErrorClass.CHECKSUM_ERROR,
    "QuotaExceeded": ErrorClass.STORAGE_ERROR,
    "EntityTooLarge": ErrorClass.STORAGE_ERROR,
}


class TransferError(Exception):
    """A classified storage failure.

    ``hint`` carries operator-facing guidance -- for CommPeak the overwhelmingly
    ``ACL_ERROR`` carries a hint that deliberately names *both* refusals
    behind CommPeak's nginx 403 -- an address missing from the account's
    Access Control List, and an address on the list that has been blocked
    anyway -- because the response cannot tell them apart and the hint used to
    assert the first.
    """

    def __init__(
        self,
        error_class: ErrorClass,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.code = code
        self.status = status
        self.hint = hint or _HINTS.get(error_class)

    @property
    def retryable(self) -> bool:
        return self.error_class.retryable

    def __str__(self) -> str:
        parts = [f"[{self.error_class}]", self.message]
        if self.code:
            parts.append(f"(code={self.code})")
        if self.hint:
            parts.append(f"-- {self.hint}")
        return " ".join(parts)


_HINTS: Final[dict[ErrorClass, str]] = {
    # Two causes, one page, and this hint must not pick a favourite.
    #
    # CommPeak's endpoint sits behind nginx, which answers a refused address
    # with its own HTML page rather than an S3 XML error. That much is solid.
    # What is *not* knowable from the response is which refusal it was: an
    # address absent from the account's IP ACL and an address that has been
    # blocked -- after repeated attempts, for instance -- produce a byte-for-
    # byte identical 403.
    #
    # This hint said "probably missing from the Access Control List" and was
    # wrong in the field. The address had been added to all eight lists, a
    # different address on the same list worked, and one of these accounts had
    # completed a full authenticated listing an hour earlier; every account
    # still got nginx's 403. Naming the ACL first sent the operator back round
    # the portal to re-check lists that were already correct. So the two causes
    # are given in the order the evidence favours: if the address is on the
    # list, believe that and look at being blocked instead.
    #
    # A previous version also recommended an unsigned `curl` as a
    # credential-free way to tell them apart. Measured: it cannot. nginx
    # refuses an unsigned request identically whether or not the address is
    # listed -- and it must, because an unsigned request names no account, so
    # there is no ACL to consult. Only a signed request carries the account,
    # which makes this probe the only place the distinction can appear at all.
    ErrorClass.ACL_ERROR: (
        "CommPeak refused this before the S3 layer saw it -- this is nginx's "
        "own 403, not an S3 error, so the keys are not the problem (a wrong "
        "key gives \"SignatureDoesNotMatch\" and a permission problem gives "
        "\"Access Denied\", both as S3 XML). Two different refusals look "
        "exactly like this and the response cannot tell them apart. Either "
        "this server's public IP is not on this S3 account's Access Control "
        "List (Recordings Access Accounts -> IP ACL tab, per instance, "
        "address with a /32 mask), or the address is on the list and has been "
        "blocked anyway, which is what repeated attempts in quick succession "
        "cause and it can persist for a long time. Check the list once: if "
        "the address is there -- especially if a different address on the same "
        "list works -- it is the second, so stop testing and give it time. "
        "The account's Access Summary tab logs the IP, time and error for "
        "every refused attempt and is the only place that says which it was"
    ),
    ErrorClass.AUTH_ERROR: "the S3 token/secret is wrong or has been rotated; re-enter it",
    ErrorClass.CONFIG_ERROR: (
        "endpoint, region or bucket is wrong; CommPeak requires path-style addressing "
        "against recordings.commpeak.com"
    ),
    ErrorClass.CHECKSUM_ERROR: (
        "source and destination bytes disagree; the object will be re-transferred"
    ),
    ErrorClass.STORAGE_ERROR: (
        "the destination rejected the write; check Wasabi quota and bucket policy"
    ),
}


def classify_exception(exc: BaseException) -> TransferError:
    """Map any exception raised by the storage layer onto a TransferError."""
    if isinstance(exc, TransferError):
        return exc

    code: str | None = None
    status: int | None = None
    message = str(exc) or exc.__class__.__name__

    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        err = response.get("Error") or {}
        code = err.get("Code")
        message = err.get("Message") or message
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")

    name = exc.__class__.__name__
    if code and code in _CODE_MAP:
        return TransferError(_CODE_MAP[code], message, code=code, status=status)
    if status and str(status) in _CODE_MAP:
        return TransferError(_CODE_MAP[str(status)], message, code=code, status=status)
    if name in _CODE_MAP:
        return TransferError(_CODE_MAP[name], message, code=code or name, status=status)
    if isinstance(exc, TimeoutError) or "Timeout" in name or "timed out" in message.lower():
        return TransferError(ErrorClass.NETWORK_ERROR, message, code=code or name, status=status)
    if isinstance(exc, (ConnectionError, OSError)):
        return TransferError(ErrorClass.NETWORK_ERROR, message, code=code or name, status=status)
    if status and 500 <= status < 600:
        return TransferError(ErrorClass.S3_ERROR, message, code=code, status=status)
    if status and status in (401, 403):
        return TransferError(ErrorClass.ACL_ERROR, message, code=code, status=status)
    return TransferError(ErrorClass.UNKNOWN, message, code=code or name, status=status)
