"""Initial schema: brands, connections, CDRs, recordings, queue, audit.

Adds the pieces SQLAlchemy metadata cannot express on its own and which carry
the platform's guarantees:

* ``cdrs`` and ``recordings`` as LIST-partitioned tables on ``brand_id``,
* Row-Level Security policies on every tenant-scoped table,
* an insert-only ``audit_events`` table,
* trigram and BRIN indexes sized for CDR search over tens of millions of rows.

Revision ID: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None

# Tables that carry brand_id and therefore need RLS.
BRAND_SCOPED = (
    "tenants",
    "commpeak_connections",
    "storage_destinations",
    "retention_policies",
    "cdrs",
    "recordings",
    "transfer_jobs",
    "transfer_attempts",
    "sync_runs",
    "audit_events",
)

ENUMS = {
    "recording_state": (
        "DISCOVERED",
        "QUEUED",
        "TRANSFERRING",
        "UPLOADED",
        "VERIFIED",
        "AVAILABLE",
        "FAILED",
        "SOURCE_DELETED",
        "MISSING_SOURCE",
    ),
    "job_state": ("PENDING", "CLAIMED", "RUNNING", "DONE", "FAILED", "CANCELLED"),
    "job_kind": ("TRANSFER", "VERIFY", "SIDECAR", "SOURCE_DELETE", "REPAIR"),
    "sync_run_kind": ("FULL_INVENTORY", "INCREMENTAL", "CDR_POLL", "RECONCILE", "RETENTION"),
    "connection_status": ("UNTESTED", "OK", "DEGRADED", "ERROR", "DISABLED"),
}


def upgrade() -> None:
    # pg_trgm powers partial phone-number search ("show me calls containing
    # 96077"), which a btree index cannot serve.  It ships in the contrib
    # package, so a bare server build fails here with an unhelpful message --
    # translate it into the actual fix.
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    except Exception as exc:
        raise RuntimeError(
            "pg_trgm is required for partial phone-number search but is not available. "
            "Install the contrib package on the database host "
            "(`sudo apt-get install postgresql-contrib-17`) and re-run this migration."
        ) from exc

    for name, values in ENUMS.items():
        labels = ", ".join(f"'{v}'" for v in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({labels})")

    # ---- brands ----------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("slug", sa.String(60), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("encryption_key_id", sa.String(64)),
        sa.Column("encryption_key_wrapped", sa.Text),
        sa.Column(
            "alert_config",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("commpeak_domain", sa.String(255)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("brand_id", "slug", name="uq_tenants_brand_id_slug"),
    )
    op.create_index("ix_tenants_brand_id", "tenants", ["brand_id"])

    op.create_table(
        "storage_destinations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default=sa.text("'wasabi'")),
        sa.Column("endpoint", sa.String(255), nullable=False),
        sa.Column("region", sa.String(40), nullable=False),
        sa.Column("bucket", sa.String(160), nullable=False),
        sa.Column("path_prefix", sa.String(200), nullable=False, server_default=sa.text("''")),
        sa.Column("access_key_sealed", sa.Text, nullable=False),
        sa.Column("secret_sealed", sa.Text, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="connection_status", create_type=False),
            nullable=False,
            server_default=sa.text("'UNTESTED'"),
        ),
        sa.Column("status_detail", sa.Text),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "concurrency_limit", sa.SmallInteger, nullable=False, server_default=sa.text("10")
        ),
        sa.Column("bytes_stored", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("objects_stored", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("brand_id", "name", name="uq_storage_destinations_brand_id_name"),
    )

    op.create_table(
        "commpeak_connections",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.BigInteger,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column(
            "s3_endpoint",
            sa.String(255),
            nullable=False,
            server_default=sa.text("'https://recordings.commpeak.com'"),
        ),
        sa.Column(
            "s3_region", sa.String(40), nullable=False, server_default=sa.text("'us-east-1'")
        ),
        sa.Column("s3_bucket", sa.String(120), nullable=False),
        sa.Column("s3_access_key_sealed", sa.Text, nullable=False),
        sa.Column("s3_secret_sealed", sa.Text, nullable=False),
        sa.Column("cdr_api_base", sa.String(255)),
        sa.Column("cdr_api_key_sealed", sa.Text),
        sa.Column("cdr_api_user", sa.String(120)),
        sa.Column(
            "destination_id",
            sa.BigInteger,
            sa.ForeignKey("storage_destinations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="connection_status", create_type=False),
            nullable=False,
            server_default=sa.text("'UNTESTED'"),
        ),
        sa.Column("status_detail", sa.Text),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_inventory_at", sa.DateTime(timezone=True)),
        sa.Column("inventory_cursor_hour", sa.DateTime(timezone=True)),
        sa.Column("earliest_hour", sa.DateTime(timezone=True)),
        sa.Column("last_cdr_cursor", sa.DateTime(timezone=True)),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "concurrency_limit", sa.SmallInteger, nullable=False, server_default=sa.text("5")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("brand_id", "s3_bucket", name="uq_commpeak_connections_brand_bucket"),
    )
    op.create_index(
        "ix_commpeak_connections_brand_tenant", "commpeak_connections", ["brand_id", "tenant_id"]
    )

    op.create_table(
        "retention_policies",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("offload_after_days", sa.Integer, nullable=False, server_default=sa.text("90")),
        sa.Column(
            "delete_source_after_verified",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "delete_source_grace_days", sa.Integer, nullable=False, server_default=sa.text("7")
        ),
        sa.Column("keep_archive_years", sa.Integer, nullable=False, server_default=sa.text("7")),
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("brand_id", name="uq_retention_policies_brand_id"),
        sa.CheckConstraint("offload_after_days >= 0", name="ck_retention_policies_offload_nonneg"),
    )

    # ---- partitioned CDR + recording tables ------------------------------
    # Declared in raw SQL: Alembic cannot express PARTITION BY, and the
    # partition key must be part of the primary key.
    op.execute(
        """
        CREATE TABLE cdrs (
            id                     BIGSERIAL     NOT NULL,
            brand_id               BIGINT        NOT NULL,
            connection_id          BIGINT        NOT NULL,
            tenant_id              BIGINT        NOT NULL,
            call_uuid              VARCHAR(64)   NOT NULL,
            call_id                BIGINT,
            start_at               TIMESTAMPTZ   NOT NULL,
            end_at                 TIMESTAMPTZ,
            call_duration          INTEGER,
            direction              VARCHAR(16),
            src                    VARCHAR(160),
            dst                    VARCHAR(160),
            src_norm               VARCHAR(24),
            dst_norm               VARCHAR(24),
            dst_country            VARCHAR(80),
            agent_extension        VARCHAR(40),
            agent_name             VARCHAR(160),
            caller_user            VARCHAR(160),
            client_callerid_name   VARCHAR(160),
            client_callerid_number VARCHAR(80),
            status                 VARCHAR(60),
            hangup_disposition     VARCHAR(40),
            public_recording_url   TEXT,
            raw                    JSONB         NOT NULL DEFAULT '{}',
            created_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
            updated_at             TIMESTAMPTZ   NOT NULL DEFAULT now(),
            PRIMARY KEY (brand_id, id),
            CONSTRAINT uq_cdrs_brand_conn_uuid UNIQUE (brand_id, connection_id, call_uuid)
        ) PARTITION BY LIST (brand_id)
        """
    )
    op.execute(
        """
        CREATE TABLE recordings (
            id                    BIGSERIAL    NOT NULL,
            brand_id              BIGINT       NOT NULL,
            connection_id         BIGINT       NOT NULL,
            tenant_id             BIGINT       NOT NULL,
            source_key            TEXT         NOT NULL,
            source_size           BIGINT       NOT NULL,
            source_etag           VARCHAR(80),
            source_last_modified  TIMESTAMPTZ,
            direction             VARCHAR(16),
            number                VARCHAR(40),
            extension             VARCHAR(40),
            started_at            TIMESTAMPTZ,
            uniqueid              BIGINT,
            seq                   SMALLINT     NOT NULL DEFAULT 0,
            call_group_key        VARCHAR(40),
            file_ext              VARCHAR(12),
            key_parsed_ok         BOOLEAN      NOT NULL DEFAULT false,
            cdr_id                BIGINT,
            call_uuid             VARCHAR(64),
            match_method          VARCHAR(20),
            match_confidence      DOUBLE PRECISION,
            match_ambiguous       BOOLEAN      NOT NULL DEFAULT false,
            correlated_at         TIMESTAMPTZ,
            state                 recording_state NOT NULL DEFAULT 'DISCOVERED',
            destination_id        BIGINT,
            destination_key       TEXT,
            destination_etag      VARCHAR(80),
            destination_size      BIGINT,
            checksum_sha256       VARCHAR(64),
            bytes_transferred     BIGINT       NOT NULL DEFAULT 0,
            transfer_started_at   TIMESTAMPTZ,
            transfer_completed_at TIMESTAMPTZ,
            verified_at           TIMESTAMPTZ,
            sidecar_written       BOOLEAN      NOT NULL DEFAULT false,
            source_deleted_at     TIMESTAMPTZ,
            last_error_class      VARCHAR(32),
            last_error_detail     TEXT,
            created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (brand_id, id),
            CONSTRAINT uq_recordings_brand_conn_key UNIQUE (brand_id, connection_id, source_key)
        ) PARTITION BY LIST (brand_id)
        """
    )

    # BRIN suits start_at: the column correlates strongly with physical order
    # because rows arrive in time order, and it costs a fraction of a btree at
    # tens of millions of rows.
    op.execute("CREATE INDEX ix_cdrs_start_brin ON cdrs USING brin (start_at)")
    op.execute("CREATE INDEX ix_cdrs_brand_start ON cdrs (brand_id, start_at DESC)")
    op.execute("CREATE INDEX ix_cdrs_conn_start ON cdrs (brand_id, connection_id, start_at DESC)")
    op.execute("CREATE INDEX ix_cdrs_call_id ON cdrs (brand_id, connection_id, call_id)")
    op.execute("CREATE INDEX ix_cdrs_src_norm ON cdrs (brand_id, src_norm)")
    op.execute("CREATE INDEX ix_cdrs_dst_norm ON cdrs (brand_id, dst_norm)")
    op.execute("CREATE INDEX ix_cdrs_agent ON cdrs (brand_id, agent_extension)")
    # Partial number search, e.g. "...96077...".
    op.execute("CREATE INDEX ix_cdrs_src_trgm ON cdrs USING gin (src gin_trgm_ops)")
    op.execute("CREATE INDEX ix_cdrs_dst_trgm ON cdrs USING gin (dst gin_trgm_ops)")
    # Correlation looks up candidates by tenant and a tight time range.
    op.execute(
        "CREATE INDEX ix_cdrs_correlation ON cdrs (brand_id, connection_id, start_at) "
        "INCLUDE (id, call_uuid, src_norm, dst_norm)"
    )

    op.execute("CREATE INDEX ix_recordings_state ON recordings (brand_id, state)")
    op.execute("CREATE INDEX ix_recordings_started ON recordings (brand_id, started_at DESC)")
    op.execute("CREATE INDEX ix_recordings_cdr ON recordings (brand_id, cdr_id)")
    op.execute(
        "CREATE INDEX ix_recordings_group ON recordings (brand_id, connection_id, call_group_key)"
    )
    op.execute(
        "CREATE INDEX ix_recordings_conn_state ON recordings (brand_id, connection_id, state)"
    )
    op.execute("CREATE INDEX ix_recordings_uniqueid ON recordings (brand_id, uniqueid)")
    # Retention scans ask for verified rows older than N days.
    op.execute(
        "CREATE INDEX ix_recordings_retention ON recordings (brand_id, verified_at) "
        "WHERE state IN ('AVAILABLE', 'VERIFIED')"
    )
    # Weak correlations an operator should sample-check.
    op.execute(
        "CREATE INDEX ix_recordings_review ON recordings (brand_id, match_method) "
        "WHERE match_method IN ('time_only', 'orphan') OR match_ambiguous"
    )

    # ---- queue -----------------------------------------------------------
    op.create_table(
        "transfer_jobs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.BigInteger, nullable=False),
        sa.Column("connection_id", sa.BigInteger, nullable=False),
        sa.Column("destination_id", sa.BigInteger),
        sa.Column("recording_id", sa.BigInteger, nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(name="job_kind", create_type=False),
            nullable=False,
            server_default=sa.text("'TRANSFER'"),
        ),
        sa.Column(
            "state",
            postgresql.ENUM(name="job_state", create_type=False),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("100")),
        sa.Column("attempts", sa.SmallInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("claimed_by", sa.String(120)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_class", sa.String(32)),
        sa.Column("error_detail", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("brand_id", "recording_id", "kind", name="uq_transfer_jobs_rec_kind"),
    )
    # The claim query only ever looks at runnable rows, so the index excludes
    # the tens of millions of finished ones.
    op.execute(
        "CREATE INDEX ix_transfer_jobs_runnable ON transfer_jobs "
        "(next_attempt_at, priority) WHERE state IN ('PENDING', 'CLAIMED')"
    )
    op.execute("CREATE INDEX ix_transfer_jobs_brand_state ON transfer_jobs (brand_id, state)")
    op.execute(
        "CREATE INDEX ix_transfer_jobs_lease ON transfer_jobs (claimed_at) WHERE state = 'RUNNING'"
    )

    op.create_table(
        "transfer_attempts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.BigInteger,
            sa.ForeignKey("transfer_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("brand_id", sa.BigInteger, nullable=False),
        sa.Column("worker", sa.String(120)),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("bytes_transferred", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("error_class", sa.String(32)),
        sa.Column("error_detail", sa.Text),
    )
    op.create_index("ix_transfer_attempts_job", "transfer_attempts", ["job_id", "started_at"])

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.BigInteger, nullable=False),
        sa.Column("connection_id", sa.BigInteger),
        sa.Column("kind", postgresql.ENUM(name="sync_run_kind", create_type=False), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("ok", sa.Boolean),
        sa.Column("discovered", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("queued", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("transferred", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("bytes_transferred", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column(
            "detail", sa.dialects.postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("error_detail", sa.Text),
    )
    op.create_index("ix_sync_runs_brand_kind", "sync_runs", ["brand_id", "kind", "started_at"])

    # ---- audit -----------------------------------------------------------
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("brand_id", sa.BigInteger),
        sa.Column(
            "at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("actor_user_id", sa.BigInteger),
        sa.Column("actor_label", sa.String(200)),
        sa.Column("ip", sa.dialects.postgresql.INET),
        sa.Column("user_agent", sa.Text),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("recording_id", sa.BigInteger),
        sa.Column("call_uuid", sa.String(64)),
        sa.Column(
            "detail", sa.dialects.postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.create_index("ix_audit_events_brand_at", "audit_events", ["brand_id", "at"])
    op.create_index("ix_audit_events_actor", "audit_events", ["brand_id", "actor_user_id", "at"])
    op.create_index("ix_audit_events_recording", "audit_events", ["brand_id", "recording_id"])

    # ---- roles + RLS -----------------------------------------------------
    # cprec_platform is the cross-brand role used by workers, the scheduler and
    # the reconciler. It is a role rather than an in-SQL escape hatch so the
    # privilege is visible in pg_roles and shows up in an audit.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cprec_platform') THEN
                CREATE ROLE cprec_platform;
            END IF;
        END $$
        """
    )

    for table in BRAND_SCOPED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE is essential: without it the policy does not apply to the
        # table's owner, and the application usually *is* the owner -- so the
        # isolation would silently do nothing.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # A request that forgot to scope itself must see zero rows rather than
        # every brand's -- fail closed.  Two details make that reliable:
        #   * missing_ok = true, so an unset variable yields NULL, not an error;
        #   * NULLIF(...,'') because a variable that was SET and then RESET on a
        #     pooled connection comes back as the empty string, and ''::bigint
        #     raises.  Without it, an unscoped query crashes instead of quietly
        #     returning nothing.
        # Either way the comparison is NULL, so no row matches.
        op.execute(
            f"""
            CREATE POLICY {table}_brand_isolation ON {table}
                USING (
                    brand_id = NULLIF(current_setting('cprec.brand_id', true), '')::bigint
                )
                WITH CHECK (
                    brand_id = NULLIF(current_setting('cprec.brand_id', true), '')::bigint
                )
            """
        )
        op.execute(f"CREATE POLICY {table}_platform ON {table} TO cprec_platform USING (true)")

    # audit_events is append-only: an audit trail you can edit is not one.
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC")
    op.execute(
        """
        CREATE RULE audit_events_no_update AS ON UPDATE TO audit_events DO INSTEAD NOTHING
        """
    )
    op.execute(
        """
        CREATE RULE audit_events_no_delete AS ON DELETE TO audit_events DO INSTEAD NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP RULE IF EXISTS audit_events_no_delete ON audit_events")
    op.execute("DROP RULE IF EXISTS audit_events_no_update ON audit_events")
    for table in BRAND_SCOPED:
        op.execute(f"DROP POLICY IF EXISTS {table}_platform ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation ON {table}")
    for table in (
        "audit_events",
        "sync_runs",
        "transfer_attempts",
        "transfer_jobs",
        "recordings",
        "cdrs",
        "retention_policies",
        "commpeak_connections",
        "storage_destinations",
        "tenants",
        "brands",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
