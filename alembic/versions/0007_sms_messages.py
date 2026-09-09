"""Text messages, sent and received, beside the calls.

CommPeak's TextPeak exposes these on two endpoints, not one, and they do not
return the same fields:

* ``GET /textpeak/streams/messages`` -- outgoing. Carries ``status``,
  ``sent_at``, ``delivered_at``, ``cost``, ``platform`` and ``campaign``.
* ``GET /textpeak/streams/incoming_messages`` -- incoming. Carries
  ``received_at``, ``from``/``to``, ``contact_name`` and ``message_length``,
  and has no status or cost at all, because an arrived message has neither.

One table with a ``direction`` column rather than two, because the thing an
operator wants is a conversation with a number, and that interleaves both.
The consequence is that several columns are null for one direction -- which is
honest: a delivery status on a message somebody sent *to* us is not unknown,
it is meaningless.

``status`` is stored as the string CommPeak sends. The reference documents it
only as "Delivery status" with the example ``delivered`` and gives no
enumeration, so an enum here would be a guess that rejects real data the first
time they add a state. The UI maps the values it recognises onto the four pill
meanings and shows anything else as-is.

Partitioned by brand and under the same forced RLS as calls and recordings:
these are two unrelated companies' customer messages, and the isolation rule
does not get an exception for a new table.

Revision ID: 0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sms_messages (
            id                  BIGSERIAL,
            brand_id            BIGINT       NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
            -- Nullable: TextPeak is billed per account, and an organisation may
            -- use it without any PBX connection at all.
            connection_id       BIGINT,
            message_uuid        TEXT         NOT NULL,
            direction           TEXT         NOT NULL,
            -- Outgoing only. See the module docstring: not an enum on purpose.
            status              TEXT,
            sent_at             TIMESTAMPTZ,
            delivered_at        TIMESTAMPTZ,
            received_at         TIMESTAMPTZ,
            -- The one timestamp every row has, whichever direction it went, so
            -- a single index can order the interleaved conversation view.
            occurred_at         TIMESTAMPTZ  NOT NULL,
            source_number       TEXT,
            source_name         TEXT,
            destination_number  TEXT,
            -- Digit-suffix forms, maintained on write, so searching for part of
            -- a number does not pay for normalisation per row.
            source_norm         VARCHAR(24),
            destination_norm    VARCHAR(24),
            country_code        VARCHAR(8),
            country_name        TEXT,
            contact_name        TEXT,
            body                TEXT,
            message_length      INTEGER,
            segments            INTEGER,
            cost                NUMERIC(14, 6),
            platform            TEXT,
            stream              TEXT,
            campaign            TEXT,
            conversation        TEXT,
            external_key        TEXT,
            raw                 JSONB        NOT NULL DEFAULT '{}'::jsonb,
            created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (brand_id, id),
            CONSTRAINT uq_sms_brand_uuid UNIQUE (brand_id, message_uuid),
            CONSTRAINT ck_sms_direction CHECK (direction IN ('in', 'out'))
        ) PARTITION BY LIST (brand_id)
        """
    )

    # Newest first is the default view, so the index is descending on the
    # timestamp every row has.
    op.execute("CREATE INDEX ix_sms_occurred ON sms_messages (brand_id, occurred_at DESC)")
    op.execute("CREATE INDEX ix_sms_status ON sms_messages (brand_id, status)")
    op.execute("CREATE INDEX ix_sms_direction ON sms_messages (brand_id, direction)")
    op.execute("CREATE INDEX ix_sms_dest ON sms_messages (brand_id, destination_norm)")
    op.execute("CREATE INDEX ix_sms_source ON sms_messages (brand_id, source_norm)")
    op.execute("CREATE INDEX ix_sms_conversation ON sms_messages (brand_id, conversation)")

    op.execute("ALTER TABLE sms_messages ENABLE ROW LEVEL SECURITY")
    # FORCE, or the policy does not apply to the table's owner -- and the
    # application usually is the owner, so isolation would silently do nothing.
    op.execute("ALTER TABLE sms_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY sms_messages_brand_isolation ON sms_messages
            USING (
                brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
            )
            WITH CHECK (
                brand_id = NULLIF(current_setting('c2w.brand_id', true), '')::bigint
            )
        """
    )
    op.execute("CREATE POLICY sms_messages_platform ON sms_messages TO c2w_platform USING (true)")

    # Partitions for the brands that already exist. New ones get theirs when
    # they are created, in the same place the calls partitions are made.
    op.execute(
        """
        DO $$
        DECLARE b RECORD;
        BEGIN
            FOR b IN SELECT id FROM brands LOOP
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS sms_messages_brand_%s '
                    'PARTITION OF sms_messages FOR VALUES IN (%s)', b.id, b.id
                );
            END LOOP;
        END $$
        """
    )

    # Where the last poll got to, per connection, so a restart does not re-read
    # everything. Separate from the CDR cursor because the two APIs are polled
    # independently and one being behind must not drag the other back.
    op.add_column(
        "commpeak_connections",
        sa.Column("last_sms_cursor", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "commpeak_connections",
        sa.Column("last_sms_sync_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("commpeak_connections", "last_sms_sync_at")
    op.drop_column("commpeak_connections", "last_sms_cursor")
    op.execute("DROP TABLE IF EXISTS sms_messages")
