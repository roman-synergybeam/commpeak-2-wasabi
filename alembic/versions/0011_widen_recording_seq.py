"""A recording's sequence number is a channel sequence, not a part counter.

Revision ID: 0011
Revises: 0010

`recordings.seq` was `smallint`, on the assumption -- from the documented key
`…-1636635223.0.flac` -- that it counts parts of a split recording: 0, 1, 2.

It does not. It is the FreeSWITCH channel sequence, and InterMagnum's PBX
writes six digits of it:

    recordings/2026/09/08/12/1788871734.100994-out-005551999752466-201-…flac

100994 is outside int16, so inserting that recording failed with *value out of
int16 range* and the object could not be inventoried at all. Not a silent
truncation -- an outright refusal on the first real key of that shape.

`integer` rather than `bigint`: the value is a per-channel counter that resets
with the switch, and four bytes covers more than two billion of them.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "recordings",
        "seq",
        existing_type=sa.SmallInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )


def downgrade() -> None:
    # Narrowing back would fail on any row this migration exists to allow, so
    # the values are clamped first rather than the migration exploding.
    op.execute("UPDATE recordings SET seq = 0 WHERE seq > 32767")
    op.alter_column(
        "recordings",
        "seq",
        existing_type=sa.Integer(),
        type_=sa.SmallInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
