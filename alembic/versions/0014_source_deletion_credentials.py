"""Somewhere to put credentials that can delete, kept apart from the ones that read.

Deleting at CommPeak is the one irreversible thing this system will ever do,
so the design question is not "how do we switch it on" but "what makes it
impossible until we mean it".

The answer here is that the reading path stays **incapable** of deleting
rather than configured not to. `CommPeakSource` raises `SourceIsReadOnly` from
every mutating method and keeps doing so; deletion is a different class with
different credentials, in these columns. If they are empty -- and they ship
empty -- no setting, no flag and no mistake can delete anything, because
there is nothing to authenticate with.

That also matches how the account is administered: the operator issues a
second S3 credential pair at CommPeak with delete permission, and the
read-only pair the rest of the system uses never gains it.

Sealed with the same per-brand envelope as every other credential, with AAD
binding them to this connection and this field, so a ciphertext cannot be
moved from the read columns into these.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "commpeak_connections",
        sa.Column("s3_delete_access_key_sealed", sa.Text(), nullable=True),
    )
    op.add_column(
        "commpeak_connections",
        sa.Column("s3_delete_secret_sealed", sa.Text(), nullable=True),
    )
    # When source deletion was last proved safe for this account, and by which
    # report. Written by the dry run, read by the real thing: a delete that
    # cannot point at a recent completeness check does not happen.
    op.add_column(
        "commpeak_connections",
        sa.Column("delete_dry_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "commpeak_connections",
        sa.Column("delete_dry_run_detail", sa.Text(), nullable=True),
    )
    # Per account, so turning it on is a decision about one bucket rather than
    # about the estate. Defaults false and stays false until somebody means it.
    op.add_column(
        "commpeak_connections",
        sa.Column(
            "delete_source_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    for column in (
        "delete_source_enabled",
        "delete_dry_run_detail",
        "delete_dry_run_at",
        "s3_delete_secret_sealed",
        "s3_delete_access_key_sealed",
    ):
        op.drop_column("commpeak_connections", column)
