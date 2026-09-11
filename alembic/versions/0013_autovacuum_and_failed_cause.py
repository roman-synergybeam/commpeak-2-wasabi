"""Keep the hot tables from drowning in dead tuples, and index the failure count.

Two problems, both measured on the live estate, both showing up as PostgreSQL
using four of the host's eight cores.

**The bloat.** Every recording is UPDATEd several times on its way through
`DISCOVERED -> QUEUED -> TRANSFERRING -> UPLOADED -> VERIFIED -> AVAILABLE`,
and in PostgreSQL each UPDATE leaves a dead tuple behind. At four million
recordings that is tens of millions of dead rows, and autovacuum's defaults are
nowhere near this write rate: `recordings_brand_1` was found holding
**8,340,617 dead tuples against 7,620,918 live** -- more dead than alive.

The cost is not the disk. It is that a sequential scan reads the bloat too, so
counting recordings by state read **5.2 GB** per call; and that a stale
visibility map makes an index-only scan unattractive, so the planner ignored
the perfectly good `(brand_id, state)` index and chose a parallel sequential
scan with two extra workers. The dashboard refreshes every ten seconds and a
platform administrator's view asks per organisation, so that ran twice a poll,
for ever.

`autovacuum_vacuum_scale_factor` at the default 0.2 means "wait until a fifth
of the table is dead", which on a seven-million-row table is 1.4 million dead
tuples before anything happens -- and then one pass, rate-limited by
`autovacuum_vacuum_cost_delay`, cannot catch up before the next million
arrive. 0.02 with no cost delay makes it run early and finish.

**The index.** `SELECT error_class, count(*) FROM transfer_jobs WHERE state =
'FAILED'` had no index to use, so the sync page scanned all 1.2 GB of
`transfer_jobs` -- to return **zero rows**, which is the happy case and the one
it will be answering almost always. A partial index on the failures makes the
question as cheap as the answer deserves.

Storage parameters have to be set on each **partition**, not on the
partitioned parent: autovacuum works on physical tables, and a parent holds no
rows. `cli.py` and the organisation-creation route apply the same settings to
partitions they create, so a new organisation is not born with the defaults.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

#: Early and unthrottled. These tables are churned by the transfer workers;
#: they are not the place to be gentle.
BUSY_TABLE_AUTOVACUUM = (
    "autovacuum_vacuum_scale_factor = 0.02, "
    "autovacuum_analyze_scale_factor = 0.02, "
    "autovacuum_vacuum_cost_delay = 0"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_transfer_jobs_failed_cause
            ON transfer_jobs (error_class)
            WHERE state = 'FAILED'
        """
    )
    op.execute(f"ALTER TABLE transfer_jobs SET ({BUSY_TABLE_AUTOVACUUM})")

    # Every existing partition of the churned tables. Written as a loop in
    # PL/pgSQL rather than generated here, because the set of partitions
    # depends on how many organisations exist in the database being upgraded.
    op.execute(
        f"""
        DO $$
        DECLARE part regclass;
        BEGIN
            FOR part IN
                SELECT i.inhrelid::regclass
                FROM pg_inherits i
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname IN ('recordings', 'cdrs', 'sms_messages')
            LOOP
                EXECUTE format('ALTER TABLE %s SET ({BUSY_TABLE_AUTOVACUUM})', part);
            END LOOP;
        END $$
        """  # noqa: S608 - BUSY_TABLE_AUTOVACUUM is a literal in this file
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_transfer_jobs_failed_cause")
    op.execute(
        """
        ALTER TABLE transfer_jobs
            RESET (autovacuum_vacuum_scale_factor,
                   autovacuum_analyze_scale_factor,
                   autovacuum_vacuum_cost_delay)
        """
    )
    op.execute(
        """
        DO $$
        DECLARE part regclass;
        BEGIN
            FOR part IN
                SELECT i.inhrelid::regclass
                FROM pg_inherits i
                JOIN pg_class p ON p.oid = i.inhparent
                WHERE p.relname IN ('recordings', 'cdrs', 'sms_messages')
            LOOP
                EXECUTE format(
                    'ALTER TABLE %s RESET (autovacuum_vacuum_scale_factor, '
                    'autovacuum_analyze_scale_factor, '
                    'autovacuum_vacuum_cost_delay)', part);
            END LOOP;
        END $$
        """
    )
