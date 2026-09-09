"""A person can work in several organisations, and keeps their own preferences.

Access was one organisation per account, which does not survive contact with
reality: the same operator handles calls for more than one of these companies,
and their role is not necessarily the same in each -- an admin here can be an
operator there.

So access becomes a list, with the role attached to each entry rather than to
the person. ``users.brand_id`` stays as the organisation they land in, and is
backfilled into the list.

Also adds a place for what someone has chosen for themselves -- theme, text
size, playback volume. On the account rather than in the browser, so it follows
them to the next machine.

Revision ID: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_brands",
        sa.Column(
            "user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "brand_id",
            sa.BigInteger,
            sa.ForeignKey("brands.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The role belongs to the pairing, not to the person: the same operator
        # can be an admin in one organisation and an operator in another.
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "brand_id", name="pk_user_brands"),
    )
    op.create_index("ix_user_brands_brand", "user_brands", ["brand_id"])
    op.execute(
        """
        ALTER TABLE user_brands ADD CONSTRAINT ck_user_brands_role
            CHECK (role IN ('ADMIN', 'OPERATOR'))
        """
    )

    # Whatever access exists today becomes the first entry in the list, so
    # nobody loses anything. A platform admin has no entries and needs none --
    # they reach every organisation by role.
    op.execute(
        """
        INSERT INTO user_brands (user_id, brand_id, role)
        SELECT id, brand_id, role FROM users
        WHERE brand_id IS NOT NULL AND role IN ('ADMIN', 'OPERATOR')
        ON CONFLICT DO NOTHING
        """
    )

    # What someone chose for themselves. On the account rather than in the
    # browser, so it follows them to the next machine; the browser keeps a copy
    # only to avoid a flash of the wrong theme before the page loads.
    op.add_column(
        "users",
        sa.Column(
            "preferences",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "preferences")
    op.drop_index("ix_user_brands_brand", table_name="user_brands")
    op.drop_table("user_brands")
