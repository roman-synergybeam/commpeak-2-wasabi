"""Where to reach a person, and the columns an administrator can edit.

The people page could create an account and change its role, but not correct a
misspelt address, fix a name, reset a forgotten password or record where to
message somebody. All of those are the ordinary work of running a console, and
all of them meant going to the database.

Telegram and Slack handles are columns rather than keys in ``preferences``
because they are not preferences -- they are how the platform reaches a person,
they will be read by the alert senders when routing becomes per-person, and a
JSONB key nobody can index or constrain is the wrong place for an address.

Both are nullable: most people will not have either, and an empty string and
"not set" should not be two different states.

Revision ID: 0009
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Telegram chat ids are numeric but arrive as strings and can be negative
    # for groups, so text rather than a bigint -- and no arithmetic is ever
    # done on one.
    op.add_column("users", sa.Column("telegram_chat_id", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("slack_user_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "slack_user_id")
    op.drop_column("users", "telegram_chat_id")
