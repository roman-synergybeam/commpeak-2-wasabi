"""An authenticator app as a second factor.

The account page carried a "Not built yet" panel where two-factor should be,
and the ``mfa.require_totp`` setting was a switch wired to nothing. That is a
poor state for a console holding recorded customer calls, so this adds the
columns the enrolment flow needs.

Four columns rather than one, and each earns its place:

* ``totp_secret_sealed`` -- the shared secret, sealed under the master key
  (:func:`c2w.crypto.seal_global`, AAD ``totp:<user id>``). Sealed rather than
  plain because a database dump would otherwise let the holder mint valid codes
  forever, which is precisely what the second factor is meant to prevent. It is
  not brand-scoped: the platform administrator belongs to no organisation, so
  the per-brand data keys do not apply.
* ``totp_enrolled_at`` -- NULL while a secret exists but has never been
  confirmed with a working code. Without this an interrupted enrolment leaves
  an account that demands codes from an authenticator nobody finished setting
  up, and locks the person out.
* ``totp_last_counter`` -- the newest time step already accepted, so a code
  cannot be used twice. A code is valid for thirty seconds; without this,
  anyone who sees one typed can use it for the rest of that window.
* ``totp_recovery_hashes`` -- Argon2 hashes of single-use recovery codes.
  Hashes, because they are passwords; and needed at all because a lost phone
  must not mean a lost platform administrator.

Revision ID: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret_sealed", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("totp_enrolled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("totp_last_counter", sa.BigInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "totp_recovery_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # Finding who still has to enrol is the question an administrator actually
    # asks once the requirement is switched on, and it is a whole-table scan
    # without this.
    op.create_index(
        "ix_users_totp_pending",
        "users",
        ["id"],
        unique=False,
        postgresql_where=sa.text("totp_enrolled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_users_totp_pending", table_name="users")
    op.drop_column("users", "totp_recovery_hashes")
    op.drop_column("users", "totp_last_counter")
    op.drop_column("users", "totp_enrolled_at")
    op.drop_column("users", "totp_secret_sealed")
