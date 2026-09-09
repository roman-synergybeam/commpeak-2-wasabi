"""The route a call took: country, queue, call type, the second agent, cost.

The calls page was asked for country, operator and SIP provider. Reading
CommPeak's own response schema for Search CDRs settles what of that exists:

    id, call_start, call_end, duration, bill_duration, type, destination,
    caller_id, country_name, agent_name, agent_pbxExtension,
    bridged_agent_name, bridged_agent_pbxExtension, hangup_cause,
    waiting_time, recording_link, queue_alias, queue_name, desks,
    custom_fields, cost

So:

* **country** was already modelled, as ``dst_country`` from ``country_name``.
* **operator** is the agent who handled it -- ``agent_name`` and its extension,
  both already modelled. A transferred call has a *second* agent, which was
  being thrown away; ``bridged_agent_name`` keeps it, because "who dealt with
  this call" has two answers on a transfer and showing only the first is wrong.
* **SIP provider** is not in the CDR at all. There is no carrier field and no
  trunk field. What does exist is the CommPeak account the call arrived on --
  one account per PBX or dialer, each its own trunk -- and that is already on
  every row as ``connection_id``. The calls page now shows its name.

``queue_name``, ``call_type``, ``bill_duration`` and ``cost`` are added at the
same time because they were being parsed and then dropped into ``raw``: the
whole payload is kept, so these can be backfilled from rows already stored
rather than re-fetched from CommPeak.

Revision ID: 0006
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels = None
depends_on = None

#: Every one of these is nullable: CommPeak omits fields that do not apply, and
#: a queue name on a direct extension call is meaningless rather than empty.
_COLUMNS = (
    ("call_type", sa.String(40)),
    ("queue_name", sa.String(160)),
    ("bridged_agent_name", sa.String(160)),
    ("bridged_agent_extension", sa.String(40)),
    ("bill_duration", sa.Integer()),
    ("cost", sa.Numeric(14, 6)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("cdrs", sa.Column(name, type_, nullable=True))

    # Backfill from the payload already stored, so history gains the columns
    # without going back to CommPeak for data we have.
    op.execute(
        """
        UPDATE cdrs SET
            call_type = COALESCE(call_type, NULLIF(raw->>'type', '')),
            queue_name = COALESCE(queue_name, NULLIF(raw->>'queue_name', '')),
            bridged_agent_name =
                COALESCE(bridged_agent_name, NULLIF(raw->>'bridged_agent_name', '')),
            bridged_agent_extension = COALESCE(
                bridged_agent_extension, NULLIF(raw->>'bridged_agent_pbxExtension', '')
            )
        WHERE raw <> '{}'::jsonb
        """
    )

    # Country is a column people filter on by name, and it was unindexed.
    op.create_index("ix_cdrs_country", "cdrs", ["brand_id", "dst_country"])
    op.create_index("ix_cdrs_queue", "cdrs", ["brand_id", "queue_name"])


def downgrade() -> None:
    op.drop_index("ix_cdrs_queue", table_name="cdrs")
    op.drop_index("ix_cdrs_country", table_name="cdrs")
    for name, _ in reversed(_COLUMNS):
        op.drop_column("cdrs", name)
