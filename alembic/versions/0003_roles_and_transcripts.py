"""Collapse the roles to three, and add transcript storage.

Seven roles were more than anyone asked for, and a role nobody can describe in
a sentence gets handed out by guesswork. Three remain: the platform admin, an
organisation's admin (the only role there that can delete a recording), and an
operator, who searches, listens and exports.

The transcript tables are added ahead of the recogniser that will fill them, so
the shape is settled and a later migration does not have to move data. Nothing
writes to them yet.

Revision ID: 0003
"""

from __future__ import annotations

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

#: Old role -> new. Anyone who could administer becomes an admin; everyone who
#: could only look becomes an operator. Nobody gains an ability they did not
#: have, and only RECORDING_ADMIN loses one -- delete, which it should not have
#: had separately from admin.
_REMAP = {
    "TENANT_ADMIN": "ADMIN",
    "RECORDING_ADMIN": "ADMIN",
    "SUPERVISOR": "OPERATOR",
    "AGENT": "OPERATOR",
    "AUDITOR": "OPERATOR",
    "READ_ONLY": "OPERATOR",
}


def upgrade() -> None:
    for old, new in _REMAP.items():
        op.execute(f"UPDATE users SET role = '{new}' WHERE role = '{old}'")  # noqa: S608

    # Roles are a fixed set in the software, so the database says so too: a
    # typo in a role name is otherwise an account with no permissions at all,
    # which fails as a confusing 403 rather than as a rejected write.
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_role
            CHECK (role IN ('SUPER_ADMIN', 'ADMIN', 'OPERATOR'))
        """
    )

    # ---- transcripts -----------------------------------------------------
    op.execute(
        """
        CREATE TABLE transcripts (
            id                BIGSERIAL   NOT NULL,
            brand_id          BIGINT      NOT NULL,
            recording_id      BIGINT      NOT NULL,
            cdr_id            BIGINT,
            engine            VARCHAR(40) NOT NULL,
            model             VARCHAR(40),
            language          VARCHAR(16),
            language_detected VARCHAR(16),
            confidence        DOUBLE PRECISION,
            duration_seconds  INTEGER,
            -- The whole transcript as one block, for full-text search. The
            -- per-phrase text lives in transcript_segments; this is the copy an
            -- index is built on.
            text              TEXT,
            redacted          BOOLEAN     NOT NULL DEFAULT false,
            diarized          BOOLEAN     NOT NULL DEFAULT false,
            speaker_count     SMALLINT,
            sentiment         DOUBLE PRECISION,
            keywords_hit      JSONB       NOT NULL DEFAULT '[]',
            engine_detail     JSONB       NOT NULL DEFAULT '{}',
            failed_reason     TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (brand_id, id),
            CONSTRAINT uq_transcripts_recording UNIQUE (brand_id, recording_id)
        ) PARTITION BY LIST (brand_id)
        """
    )
    op.execute(
        """
        CREATE TABLE transcript_segments (
            id            BIGSERIAL   NOT NULL,
            brand_id      BIGINT      NOT NULL,
            transcript_id BIGINT      NOT NULL,
            seq           INTEGER     NOT NULL,
            -- Offsets into the recording, so a phrase found by searching can be
            -- played rather than only read.
            start_ms      INTEGER     NOT NULL,
            end_ms        INTEGER     NOT NULL,
            speaker       VARCHAR(40),
            -- The agent's name where the call has an extension we can match,
            -- so a transcript reads as a conversation between named people.
            speaker_name  VARCHAR(160),
            text          TEXT        NOT NULL,
            confidence    DOUBLE PRECISION,
            PRIMARY KEY (brand_id, id)
        ) PARTITION BY LIST (brand_id)
        """
    )

    op.execute("CREATE INDEX ix_transcripts_recording ON transcripts (brand_id, recording_id)")
    op.execute("CREATE INDEX ix_transcripts_cdr ON transcripts (brand_id, cdr_id)")
    op.execute(
        "CREATE INDEX ix_transcripts_language ON transcripts (brand_id, language_detected)"
    )
    # Searching what was said is the point of storing it. Portuguese and
    # Spanish stemming matter here: 'simple' would not match plurals or verb
    # endings, and these calls are mostly in those two languages.
    op.execute(
        "CREATE INDEX ix_transcripts_text_en ON transcripts "
        "USING gin (to_tsvector('english', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX ix_transcripts_text_es ON transcripts "
        "USING gin (to_tsvector('spanish', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX ix_transcripts_text_pt ON transcripts "
        "USING gin (to_tsvector('portuguese', coalesce(text, '')))"
    )
    op.execute(
        "CREATE INDEX ix_transcript_segments_parent ON transcript_segments "
        "(brand_id, transcript_id, seq)"
    )

    for table in ("transcripts", "transcript_segments"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_brand_isolation ON {table}
                USING (brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint)
                WITH CHECK (
                    brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
                )
            """
        )
        op.execute(f"CREATE POLICY {table}_platform ON {table} TO c2w_platform USING (true)")

    # A transcript is a job like any other, so it goes through the same queue.
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'TRANSCRIBE'")
    op.execute("ALTER TYPE job_kind ADD VALUE IF NOT EXISTS 'ANALYSE'")
    op.execute("ALTER TYPE sync_run_kind ADD VALUE IF NOT EXISTS 'TRANSCRIBE'")


def downgrade() -> None:
    for table in ("transcript_segments", "transcripts"):
        op.execute(f"DROP POLICY IF EXISTS {table}_platform ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_brand_isolation ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role")
    # The old role names are not restored: the information needed to tell a
    # supervisor from an auditor was discarded by the remap above.
