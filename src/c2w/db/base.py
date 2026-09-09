"""Declarative base and shared column conventions."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now_column(**kw) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class RecordingState(enum.StrEnum):
    """Lifecycle of one recording object.

    ``AVAILABLE`` is set only after byte-level verification against the
    destination -- an HTTP 200 from Wasabi is not sufficient evidence that the
    archive copy is good.
    """

    DISCOVERED = "DISCOVERED"
    QUEUED = "QUEUED"
    TRANSFERRING = "TRANSFERRING"
    UPLOADED = "UPLOADED"
    VERIFIED = "VERIFIED"
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"
    #: Verified in the archive and deleted at source, by explicit opt-in.
    SOURCE_DELETED = "SOURCE_DELETED"
    #: Present in our inventory but gone from the source before we copied it.
    MISSING_SOURCE = "MISSING_SOURCE"

    @property
    def playable(self) -> bool:
        return self in (RecordingState.AVAILABLE, RecordingState.SOURCE_DELETED)

    @property
    def terminal(self) -> bool:
        return self in (
            RecordingState.AVAILABLE,
            RecordingState.SOURCE_DELETED,
            RecordingState.FAILED,
            RecordingState.MISSING_SOURCE,
        )


class JobState(enum.StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobKind(enum.StrEnum):
    TRANSFER = "TRANSFER"
    VERIFY = "VERIFY"
    SIDECAR = "SIDECAR"
    SOURCE_DELETE = "SOURCE_DELETE"
    REPAIR = "REPAIR"


class SyncRunKind(enum.StrEnum):
    FULL_INVENTORY = "FULL_INVENTORY"
    INCREMENTAL = "INCREMENTAL"
    CDR_POLL = "CDR_POLL"
    RECONCILE = "RECONCILE"
    RETENTION = "RETENTION"


class ConnectionStatus(enum.StrEnum):
    UNTESTED = "UNTESTED"
    OK = "OK"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    DISABLED = "DISABLED"
