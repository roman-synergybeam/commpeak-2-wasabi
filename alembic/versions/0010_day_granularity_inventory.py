"""Inventory works in days, because the buckets are organised in days.

Revision ID: 0010
Revises: 0009

The scanner listed `YYYY/MM/DD/HH/` prefixes, from CommPeak's published key
layout. Measured against all eight live buckets, real keys are
`recordings/YYYY/MM/DD/<basename>`: a root prefix the documentation omits, and
no hour level at all.

So these two columns held an hour boundary for a tree that has no hours. Their
names are the point of this migration -- a column called
`inventory_cursor_hour` storing a midnight is the sort of quiet lie that costs
somebody an afternoon later. The values are preserved: an hour timestamp is a
valid day timestamp for resumption purposes, and the incremental planner
truncates to midnight anyway.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "commpeak_connections",
        "inventory_cursor_hour",
        new_column_name="inventory_cursor_day",
    )
    op.alter_column(
        "commpeak_connections",
        "earliest_hour",
        new_column_name="earliest_day",
    )


def downgrade() -> None:
    op.alter_column(
        "commpeak_connections",
        "inventory_cursor_day",
        new_column_name="inventory_cursor_hour",
    )
    op.alter_column(
        "commpeak_connections",
        "earliest_day",
        new_column_name="earliest_hour",
    )
