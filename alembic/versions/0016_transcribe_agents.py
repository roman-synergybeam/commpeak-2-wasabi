"""Remote transcription agents: who they are, what they hold, how far they got.

The server has eight cores and no GPU, which transcribes about a call a minute
against an archive of twenty-four million. A machine elsewhere with a GPU can
do the recognising instead, if it can be handed work without being handed the
keys to the archive. These tables are that hand-off:

``transcribe_agents``
    One row per machine. It authenticates with a bearer token of which only
    the SHA-256 is stored, and is switched off by setting ``revoked_at``. Not
    brand-scoped: an agent works for the platform, and the server -- not the
    agent -- decides which organisation each piece of work belongs to.

``transcribe_leases``
    A recording currently handed to an agent, so that two agents (or an agent
    and the server's own transcriber) do not recognise the same call. Expires
    on its own, so a machine that is switched off mid-batch gives its work
    back without anyone noticing it had any.

``transcribe_cursors``
    Where each organisation's agent work has reached, oldest first. Without
    it, every claim would scan from the oldest recording past every one
    already transcribed -- millions of index probes per claim by the end.

Leases and cursors are brand-scoped under the same row-level security as every
other organisation's data.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_RLS_TABLES = ("transcribe_leases", "transcribe_cursors")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE transcribe_agents (
            id            BIGSERIAL PRIMARY KEY,
            name          VARCHAR(120) NOT NULL UNIQUE,
            token_hash    CHAR(64)     NOT NULL UNIQUE,
            created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            last_seen_at  TIMESTAMPTZ,
            last_detail   JSONB        NOT NULL DEFAULT '{}'::jsonb,
            revoked_at    TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE transcribe_leases (
            brand_id      BIGINT       NOT NULL REFERENCES brands (id),
            recording_id  BIGINT       NOT NULL,
            agent_id      BIGINT       NOT NULL REFERENCES transcribe_agents (id),
            token         VARCHAR(64)  NOT NULL UNIQUE,
            started_at    TIMESTAMPTZ,
            leased_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ  NOT NULL,
            PRIMARY KEY (brand_id, recording_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_transcribe_leases_expiry ON transcribe_leases (brand_id, expires_at)")
    op.execute(
        """
        CREATE TABLE transcribe_cursors (
            brand_id      BIGINT       PRIMARY KEY REFERENCES brands (id),
            started_from  TIMESTAMPTZ,
            updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )

    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE, or the policy does not apply to the table's owner -- and the
        # application is the owner.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_brand_isolation ON {table}
                USING (
                    brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
                )
                WITH CHECK (
                    brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
                )
            """
        )
        op.execute(f"CREATE POLICY {table}_platform ON {table} TO c2w_platform USING (true)")

    # The progress figures count what was finished in the last hour and day.
    # No new index on `recordings` for the agent's oldest-first scan: the
    # existing `(brand_id, started_at DESC)` serves it read backwards, and
    # building one on a twenty-gigabyte partition blocks the copy while it runs.
    op.execute("CREATE INDEX ix_transcripts_updated ON transcripts (brand_id, updated_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transcripts_updated")
    op.execute("DROP TABLE IF EXISTS transcribe_cursors")
    op.execute("DROP TABLE IF EXISTS transcribe_leases")
    op.execute("DROP TABLE IF EXISTS transcribe_agents")
