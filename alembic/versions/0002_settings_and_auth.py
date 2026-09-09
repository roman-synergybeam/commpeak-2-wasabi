"""Settings storage, users, sessions; drop the source-deletion column.

Moves all runtime configuration into the database (``app_settings``), adds local
authentication (``users``, ``user_sessions``) ahead of the AD integration, and
removes the per-brand source-deletion flag: while the platform is read-only
against CommPeak there must be no column that can switch it on.

Revision ID: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- settings --------------------------------------------------------
    op.create_table(
        "app_settings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column(
            "brand_id", sa.BigInteger, sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("value", postgresql.JSONB),
        sa.Column("is_sealed", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()",
            ),
        ),
        sa.Column("updated_by", sa.String(200)),
        sa.Column("note", sa.Text),
    )
    op.create_index("ix_app_settings_key", "app_settings", ["key"])
    # A plain UNIQUE(key, brand_id) would not constrain the global rows, because
    # NULL never equals NULL in a unique index. Two partial indexes do the job:
    # one global row per key, and one row per key per brand.
    op.execute(
        "CREATE UNIQUE INDEX uq_app_settings_global ON app_settings (key) WHERE brand_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_app_settings_brand ON app_settings (key, brand_id) "
        "WHERE brand_id IS NOT NULL"
    )

    op.create_table(
        "setting_history",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(120), nullable=False),
        sa.Column("brand_id", sa.BigInteger),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("old_value", postgresql.JSONB),
        sa.Column("new_value", postgresql.JSONB),
        sa.Column("changed_by", sa.String(200)),
        sa.Column("note", sa.Text),
    )
    op.create_index("ix_setting_history_key_at", "setting_history", ["key", "at"])
    # Configuration history is evidence of what changed and when; like the audit
    # log, it is append-only.
    op.execute("REVOKE UPDATE, DELETE ON setting_history FROM PUBLIC")
    op.execute(
        "CREATE RULE setting_history_no_update AS ON UPDATE TO setting_history DO INSTEAD NOTHING"
    )
    op.execute(
        "CREATE RULE setting_history_no_delete AS ON DELETE TO setting_history DO INSTEAD NOTHING"
    )

    # ---- users -----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # NULL for SUPER_ADMIN only: that role administers every brand.
        sa.Column(
            "brand_id", sa.BigInteger, sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200)),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("auth_source", sa.String(16), nullable=False, server_default=sa.text("'LOCAL'")),
        sa.Column("password_hash", sa.Text),
        sa.Column(
            "must_change_password", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column("oidc_issuer", sa.String(255)),
        sa.Column("oidc_subject", sa.String(255)),
        sa.Column("oidc_groups", postgresql.JSONB),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("failed_logins", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()",
            ),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()",
            ),
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_brand", "users", ["brand_id"])
    op.create_index("ix_users_oidc", "users", ["oidc_issuer", "oidc_subject"], unique=True)
    # Only SUPER_ADMIN may be brand-less; anyone else without a brand would sit
    # outside the isolation model entirely.
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_brand_required
            CHECK (role = 'SUPER_ADMIN' OR brand_id IS NOT NULL)
        """
    )
    # A local account needs a password; a federated one must not carry a local
    # password hash at all.
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_credentials_match_source
            CHECK (
                (auth_source = 'LOCAL'  AND password_hash IS NOT NULL)
             OR (auth_source <> 'LOCAL' AND password_hash IS NULL AND oidc_subject IS NOT NULL)
            )
        """
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text(
                "now()",
            ),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("ip", postgresql.INET),
        sa.Column("user_agent", sa.Text),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_user_sessions_user", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_expires", "user_sessions", ["expires_at"])

    # ---- remove the ability to delete at source --------------------------
    # The platform is strictly read-only against CommPeak. Leaving a per-brand
    # "delete source after verified" flag in the schema would mean a single UPDATE
    # could arm irreversible deletion, so the column and its grace period are
    # dropped outright. Re-adding them is a deliberate future migration, not a
    # configuration change.
    op.drop_column("retention_policies", "delete_source_after_verified")
    op.drop_column("retention_policies", "delete_source_grace_days")


def downgrade() -> None:
    op.add_column(
        "retention_policies",
        sa.Column(
            "delete_source_grace_days", sa.Integer, nullable=False, server_default=sa.text("7")
        ),
    )
    op.add_column(
        "retention_policies",
        sa.Column(
            "delete_source_after_verified",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.drop_table("user_sessions")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_credentials_match_source")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_brand_required")
    op.drop_table("users")
    op.execute("DROP RULE IF EXISTS setting_history_no_delete ON setting_history")
    op.execute("DROP RULE IF EXISTS setting_history_no_update ON setting_history")
    op.drop_table("setting_history")
    op.drop_table("app_settings")
