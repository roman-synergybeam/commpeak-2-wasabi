"""An index that matches how the queue is actually claimed.

The claim filters `connection_id` and `state`, and orders by
`priority, next_attempt_at`. The only index that came close was
`ix_transfer_jobs_runnable (next_attempt_at, priority)` on
`state IN ('PENDING','CLAIMED')`, which leads with a column the claim does not
restrict and omits the one it does -- so with nine million pending jobs the
planner gave up on it entirely and chose a **parallel sequential scan**.

Measured on the live queue before this index, for a claim of eight jobs:

    Limit  (actual time=730.979..732.283 rows=8 loops=1)
      ->  Gather Merge  (Workers Launched: 2)
            ->  Parallel Seq Scan on transfer_jobs
                  (actual rows=1773211 loops=3)
                  Rows Removed by Filter: 1396181
    Buffers: shared read=149635
    Execution Time: 732.325 ms

Five worker processes claim once per account and there are eight accounts, so
that is forty of these per cycle, each reading five million rows across three
parallel workers to return eight. `pg_stat_user_tables` had recorded 165,591
sequential scans over this table totalling **351 billion rows read**, and
PostgreSQL was using four of the host's eight cores doing nothing else.

`(connection_id, priority, next_attempt_at)` makes it an ordered range scan:
`connection_id` positions, the remaining two supply the `ORDER BY` so there is
no sort, and `LIMIT` stops after a handful of entries. Partial on
`state = 'PENDING'` because that predicate matches the claim's own literal and
keeps the index to the rows that can still be claimed -- an entry leaves it as
soon as a job starts running.

`kind` is deliberately not in it. Every job on this estate is `TRANSFER`, so it
would add a column of no selectivity, and putting it before `priority` would
break the ordering the `ORDER BY` gets for free.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Not CONCURRENTLY: the scheduler and reconciler each hold a transaction
    # open for the life of the process to keep an advisory lock, and
    # CREATE INDEX CONCURRENTLY waits for every concurrent transaction to
    # finish -- so it would wait for those for ever. A plain build takes an
    # ACCESS EXCLUSIVE lock for a minute or two on a queue table, which is a
    # brief stall in work that is resumable by construction.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_transfer_jobs_claim
            ON transfer_jobs (connection_id, priority, next_attempt_at)
            WHERE state = 'PENDING'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transfer_jobs_claim")
