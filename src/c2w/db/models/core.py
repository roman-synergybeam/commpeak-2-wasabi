"""Schema.

Isolation model: ``brand`` is the hard boundary between unrelated companies
(Go4Rex and InterMagnum share no data by design).  Every tenant-scoped table
carries ``brand_id`` and is protected by PostgreSQL Row-Level Security keyed off
the ``c2w.brand_id`` session variable.  Application ``WHERE`` clauses are a
convenience; RLS is the control.  See ``alembic/versions`` for the policies.

``cdrs`` and ``recordings`` are declaratively partitioned by ``brand_id`` --
this both reinforces isolation and keeps search fast at the ~19M-object scale
these buckets already hold.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from c2w.db.base import (
    Base,
    ConnectionStatus,
    IdMixin,
    JobKind,
    JobState,
    RecordingState,
    SyncRunKind,
    TimestampMixin,
)

_recording_state = ENUM(RecordingState, name="recording_state", create_type=False)
_job_state = ENUM(JobState, name="job_state", create_type=False)
_job_kind = ENUM(JobKind, name="job_kind", create_type=False)
_sync_kind = ENUM(SyncRunKind, name="sync_run_kind", create_type=False)
_conn_status = ENUM(ConnectionStatus, name="connection_status", create_type=False)


class Brand(Base, IdMixin, TimestampMixin):
    """A customer company.  The isolation boundary."""

    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    #: Envelope-encryption data key for this brand's stored credentials.  Held
    #: per brand so rotating one company's keys never touches another's rows.
    encryption_key_id: Mapped[str | None] = mapped_column(String(64))
    encryption_key_wrapped: Mapped[str | None] = mapped_column(Text)

    #: Telegram/Slack routing. Chat ids and webhook URLs are credentials, so
    #: they are sealed before being stored here.
    alert_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    tenants: Mapped[list[Tenant]] = relationship(back_populates="brand")


class Tenant(Base, IdMixin, TimestampMixin):
    """One CommPeak domain/PBX within a brand, e.g. ``go4rex.pbx.commpeak.com``."""

    __tablename__ = "tenants"
    __table_args__ = (UniqueConstraint("brand_id", "slug"),)

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    commpeak_domain: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    brand: Mapped[Brand] = relationship(back_populates="tenants")


class CommPeakConnection(Base, IdMixin, TimestampMixin):
    """One CommPeak S3 account plus its CDR API access.

    Requirement 3 ("CDR per bucket") lands here: a connection is the unit that
    owns both a bucket and the CDR feed for that bucket, so CDR views can always
    be scoped to exactly one bucket.
    """

    __tablename__ = "commpeak_connections"
    __table_args__ = (
        UniqueConstraint("brand_id", "s3_bucket"),
        Index("ix_commpeak_connections_brand_tenant", "brand_id", "tenant_id"),
    )

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    s3_endpoint: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("'https://recordings.commpeak.com'")
    )
    s3_region: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        server_default=text("'us-east-1'"),
    )
    s3_bucket: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Sealed by c2w.crypto under the brand's data key. Never logged, never
    #: rendered into a template, never returned by the API.
    s3_access_key_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    s3_secret_sealed: Mapped[str] = mapped_column(Text, nullable=False)

    cdr_api_base: Mapped[str | None] = mapped_column(String(255))
    cdr_api_key_sealed: Mapped[str | None] = mapped_column(Text)
    cdr_api_user: Mapped[str | None] = mapped_column(String(120))

    #: Destination this connection's recordings are archived to.
    destination_id: Mapped[int | None] = mapped_column(
        ForeignKey("storage_destinations.id", ondelete="SET NULL")
    )

    status: Mapped[ConnectionStatus] = mapped_column(
        _conn_status, nullable=False, server_default=text("'UNTESTED'")
    )
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_inventory_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Newest day prefix fully inventoried; incremental scans resume from here
    #: (minus an overlap) instead of re-walking millions of objects. A day
    #: because that is the deepest folder CommPeak buckets actually have --
    #: these were named `_hour` and listed an hour level that does not exist.
    inventory_cursor_day: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Oldest day known to exist, discovered by prefix descent.
    earliest_day: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_cdr_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    concurrency_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5")
    )


class StorageDestination(Base, IdMixin, TimestampMixin):
    """An S3-compatible archive target.

    Modelled as a generic provider rather than "a Wasabi account" so a brand can
    be moved to another S3 service without touching the engine.
    """

    __tablename__ = "storage_destinations"
    __table_args__ = (UniqueConstraint("brand_id", "name"),)

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        server_default=text("'wasabi'"),
    )
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(40), nullable=False)
    bucket: Mapped[str] = mapped_column(String(160), nullable=False)
    path_prefix: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("''"))
    access_key_sealed: Mapped[str] = mapped_column(Text, nullable=False)
    secret_sealed: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ConnectionStatus] = mapped_column(
        _conn_status, nullable=False, server_default=text("'UNTESTED'")
    )
    status_detail: Mapped[str | None] = mapped_column(Text)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    concurrency_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10")
    )
    bytes_stored: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    objects_stored: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )


class RetentionPolicy(Base, IdMixin, TimestampMixin):
    """Per-brand retention rules.

    There is deliberately no "delete at source" flag here.  The platform is
    read-only against CommPeak, and a column that could arm irreversible
    deletion with a single UPDATE has no business existing while that is true.
    """

    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("brand_id"),
        CheckConstraint("offload_after_days >= 0", name="offload_after_days_nonneg"),
    )

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    offload_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90")
    )
    #: How long an archived recording is kept. **Zero means forever**, and the
    #: obvious implementation of the opposite -- a `now - years` cutoff -- turns
    #: that into "delete everything immediately", which is the exact reverse of
    #: what the operator selected. Nothing acts on this column yet; whatever
    #: eventually does must special-case 0 before computing any cutoff.
    keep_archive_years: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("7"),
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Cdr(Base, TimestampMixin):
    """Call detail record, as polled from the CommPeak CDR API.

    Partitioned by ``brand_id``; ``raw`` keeps the untouched API payload so a
    later schema change never loses fields we did not model yet.
    """

    __tablename__ = "cdrs"
    __table_args__ = (
        UniqueConstraint("brand_id", "connection_id", "call_uuid"),
        Index("ix_cdrs_brand_start", "brand_id", "start_at"),
        Index("ix_cdrs_conn_start", "brand_id", "connection_id", "start_at"),
        Index("ix_cdrs_call_id", "brand_id", "connection_id", "call_id"),
        {"postgresql_partition_by": "LIST (brand_id)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    connection_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    call_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    call_id: Mapped[int | None] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    call_duration: Mapped[int | None] = mapped_column(Integer)

    direction: Mapped[str | None] = mapped_column(String(16))
    src: Mapped[str | None] = mapped_column(String(160))
    dst: Mapped[str | None] = mapped_column(String(160))
    #: Digit-suffix forms of src/dst, maintained on write so number search and
    #: correlation never pay for per-row normalisation.
    src_norm: Mapped[str | None] = mapped_column(String(24))
    dst_norm: Mapped[str | None] = mapped_column(String(24))
    dst_country: Mapped[str | None] = mapped_column(String(80))

    agent_extension: Mapped[str | None] = mapped_column(String(40))
    agent_name: Mapped[str | None] = mapped_column(String(160))
    #: A transfer has a second agent. "Who handled this call" then has two
    #: answers, and showing only the first is wrong.
    bridged_agent_name: Mapped[str | None] = mapped_column(String(160))
    bridged_agent_extension: Mapped[str | None] = mapped_column(String(40))
    #: CommPeak's own ``type``: what kind of call this was.
    call_type: Mapped[str | None] = mapped_column(String(40))
    queue_name: Mapped[str | None] = mapped_column(String(160))
    #: Billed seconds, which is not the same as elapsed seconds.
    bill_duration: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    caller_user: Mapped[str | None] = mapped_column(String(160))
    client_callerid_name: Mapped[str | None] = mapped_column(String(160))
    client_callerid_number: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str | None] = mapped_column(String(60))
    hangup_disposition: Mapped[str | None] = mapped_column(String(40))
    public_recording_url: Mapped[str | None] = mapped_column(Text)

    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))


class SmsMessage(Base, TimestampMixin):
    """One text message, in either direction.

    Sent and received messages share a table because what an operator wants is
    the exchange with a number, and that interleaves the two. The cost is that
    several columns apply to only one direction -- ``status``/``delivered_at``
    for outgoing, ``received_at``/``contact_name`` for incoming. That is
    honest: a delivery status on a message sent *to* us is meaningless rather
    than merely unknown.

    ``occurred_at`` is the one timestamp every row has, whichever direction it
    went, so a single index orders the combined view.
    """

    __tablename__ = "sms_messages"
    __table_args__ = (
        UniqueConstraint("brand_id", "message_uuid", name="uq_sms_brand_uuid"),
        CheckConstraint("direction IN ('in', 'out')", name="ck_sms_direction"),
        {"postgresql_partition_by": "LIST (brand_id)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    connection_id: Mapped[int | None] = mapped_column(BigInteger)

    message_uuid: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    #: As CommPeak sends it. Not an enum: the reference gives no enumeration,
    #: so one here would reject real data the first time a state is added.
    status: Mapped[str | None] = mapped_column(Text)

    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source_number: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(Text)
    destination_number: Mapped[str | None] = mapped_column(Text)
    source_norm: Mapped[str | None] = mapped_column(String(24))
    destination_norm: Mapped[str | None] = mapped_column(String(24))

    country_code: Mapped[str | None] = mapped_column(String(8))
    country_name: Mapped[str | None] = mapped_column(Text)
    contact_name: Mapped[str | None] = mapped_column(Text)

    body: Mapped[str | None] = mapped_column(Text)
    message_length: Mapped[int | None] = mapped_column(Integer)
    #: A long message is billed as several parts; CommPeak charges per part.
    segments: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))

    platform: Mapped[str | None] = mapped_column(Text)
    stream: Mapped[str | None] = mapped_column(Text)
    campaign: Mapped[str | None] = mapped_column(Text)
    conversation: Mapped[str | None] = mapped_column(Text)
    external_key: Mapped[str | None] = mapped_column(Text)

    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))


class Recording(Base, TimestampMixin):
    """One recording object, from discovery through to verified archive."""

    __tablename__ = "recordings"
    __table_args__ = (
        UniqueConstraint("brand_id", "connection_id", "source_key"),
        Index("ix_recordings_state", "brand_id", "state"),
        Index("ix_recordings_started", "brand_id", "started_at"),
        Index("ix_recordings_cdr", "brand_id", "cdr_id"),
        Index("ix_recordings_group", "brand_id", "connection_id", "call_group_key"),
        Index("ix_recordings_conn_state", "brand_id", "connection_id", "state"),
        {"postgresql_partition_by": "LIST (brand_id)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    brand_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    connection_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # -- source side --------------------------------------------------------
    source_key: Mapped[str] = mapped_column(Text, nullable=False)
    source_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_etag: Mapped[str | None] = mapped_column(String(80))
    source_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- parsed from the object key ----------------------------------------
    direction: Mapped[str | None] = mapped_column(String(16))
    number: Mapped[str | None] = mapped_column(String(40))
    extension: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uniqueid: Mapped[int | None] = mapped_column(BigInteger)
    #: The part number within a call. `SmallInteger` was too narrow: it is a
    #: FreeSWITCH channel sequence, not a 0/1/2 part counter, and InterMagnum's
    #: PBX writes six-digit values -- 100994 overflows int16 and the insert
    #: fails outright, so the first real recording of that shape could not be
    #: stored at all.
    seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    call_group_key: Mapped[str | None] = mapped_column(String(40))
    file_ext: Mapped[str | None] = mapped_column(String(12))
    key_parsed_ok: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
    )

    # -- correlation --------------------------------------------------------
    cdr_id: Mapped[int | None] = mapped_column(BigInteger)
    call_uuid: Mapped[str | None] = mapped_column(String(64))
    match_method: Mapped[str | None] = mapped_column(String(20))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    match_ambiguous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    correlated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # -- archive side -------------------------------------------------------
    state: Mapped[RecordingState] = mapped_column(
        _recording_state, nullable=False, server_default=text("'DISCOVERED'")
    )
    destination_id: Mapped[int | None] = mapped_column(BigInteger)
    destination_key: Mapped[str | None] = mapped_column(Text)
    destination_etag: Mapped[str | None] = mapped_column(String(80))
    destination_size: Mapped[int | None] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    transfer_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfer_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sidecar_written: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    source_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_class: Mapped[str | None] = mapped_column(String(32))
    last_error_detail: Mapped[str | None] = mapped_column(Text)


class TransferJob(Base, IdMixin, TimestampMixin):
    """Queue row.

    Claimed with ``FOR UPDATE SKIP LOCKED``.  PostgreSQL is the queue rather
    than Redis: the deployment forbids containers and extra daemons, and this
    way a job's state change and its recording's state change commit in one
    transaction, so a worker crash can never leave the two disagreeing.
    """

    __tablename__ = "transfer_jobs"
    __table_args__ = (
        UniqueConstraint("brand_id", "recording_id", "kind"),
        # Partial index over runnable work only -- the queue stays fast even
        # with tens of millions of completed rows behind it.
        Index(
            "ix_transfer_jobs_runnable",
            "next_attempt_at",
            "priority",
            postgresql_where=text("state IN ('PENDING','CLAIMED')"),
        ),
        Index("ix_transfer_jobs_brand_state", "brand_id", "state"),
        Index("ix_transfer_jobs_lease", "claimed_at", postgresql_where=text("state = 'RUNNING'")),
    )

    brand_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    connection_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    destination_id: Mapped[int | None] = mapped_column(BigInteger)
    recording_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[JobKind] = mapped_column(
        _job_kind,
        nullable=False,
        server_default=text("'TRANSFER'"),
    )
    state: Mapped[JobState] = mapped_column(
        _job_state,
        nullable=False,
        server_default=text("'PENDING'"),
    )
    #: Lower runs first. The backfill uses recency so the newest month drains
    #: before years of history.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)


class TransferAttempt(Base, IdMixin):
    """One attempt at a job; the audit trail behind a failure."""

    __tablename__ = "transfer_attempts"
    __table_args__ = (Index("ix_transfer_attempts_job", "job_id", "started_at"),)

    job_id: Mapped[int] = mapped_column(
        ForeignKey("transfer_jobs.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    worker: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    error_class: Mapped[str | None] = mapped_column(String(32))
    error_detail: Mapped[str | None] = mapped_column(Text)


class SyncRun(Base, IdMixin):
    """One execution of a scheduled activity, with its counters."""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_brand_kind", "brand_id", "kind", "started_at"),)

    brand_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    connection_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[SyncRunKind] = mapped_column(_sync_kind, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    discovered: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    queued: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    transferred: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    failed: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("0"),
    )
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    error_detail: Mapped[str | None] = mapped_column(Text)


class AuditEvent(Base, IdMixin):
    """Append-only audit log.

    These are call recordings, so who listened to what is itself sensitive and
    frequently subject to compliance review.  Deletes and updates are revoked
    from the application role in the migration; the table is insert-only.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_brand_at", "brand_id", "at"),
        Index("ix_audit_events_actor", "brand_id", "actor_user_id", "at"),
        Index("ix_audit_events_recording", "brand_id", "recording_id"),
    )

    brand_id: Mapped[int | None] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_label: Mapped[str | None] = mapped_column(String(200))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False)
    recording_id: Mapped[int | None] = mapped_column(BigInteger)
    call_uuid: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
