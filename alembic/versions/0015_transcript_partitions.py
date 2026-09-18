"""The partitions migration 0003 declared but never created.

`transcripts` and `transcript_segments` were both created `PARTITION BY LIST
(brand_id)` and no partition was ever made for them -- not in 0003 for the
brands that already existed, and not in either of the two places that create
partitions when a brand is added, because both of those write the table names
out by hand and neither was updated.

A partitioned parent with no children accepts no rows at all. So the
transcriber ran, downloaded the audio, loaded the model, recognised the call,
and then threw the result away on the INSERT:

    CheckViolationError: no partition of relation "transcripts" found for row
    DETAIL:  Partition key of the failing row contains (brand_id) = (2).

once per recording, logged as `transcribe.pass_failed` and visible nowhere
else, which is why it went unnoticed while the transcripts table sat empty.

The lasting half of the fix is `BRAND_PARTITIONED_TABLES` in `db/base.py`:
both creation sites now read the names from there, so the next partitioned
table cannot be added to the schema and forgotten by the code that has to
provision it.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

#: Matches `PARTITION_AUTOVACUUM` in `db/base.py`, restated here because a
#: migration must not import from application code that can change under it.
_AUTOVACUUM = (
    "autovacuum_vacuum_scale_factor = 0.02, "
    "autovacuum_analyze_scale_factor = 0.02, "
    "autovacuum_vacuum_cost_delay = 0"
)

_TABLES = ("transcripts", "transcript_segments")


def upgrade() -> None:
    # `IF NOT EXISTS` because a partition may already have been created by
    # hand on a host where this was diagnosed before the migration was written.
    for table in _TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE b RECORD;
            BEGIN
                FOR b IN SELECT id FROM brands LOOP
                    EXECUTE format(
                        'CREATE TABLE IF NOT EXISTS {table}_brand_%s '
                        'PARTITION OF {table} FOR VALUES IN (%s)', b.id, b.id
                    );
                    EXECUTE format(
                        'ALTER TABLE {table}_brand_%s SET ({_AUTOVACUUM})', b.id
                    );
                END LOOP;
            END $$
            """  # noqa: S608 - `table` comes from `_TABLES`, a literal in this file
        )


def downgrade() -> None:
    # Detach rather than drop: a partition holds the only copy of transcripts
    # that cost hours of CPU to produce, and a downgrade is not a reason to
    # destroy them. They are left as free-standing tables.
    for table in _TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE b RECORD;
            BEGIN
                FOR b IN SELECT id FROM brands LOOP
                    EXECUTE format(
                        'ALTER TABLE {table} DETACH PARTITION {table}_brand_%s', b.id
                    );
                END LOOP;
            EXCEPTION WHEN undefined_table THEN NULL;
            END $$
            """  # noqa: S608 - `table` comes from `_TABLES`, a literal in this file
        )
