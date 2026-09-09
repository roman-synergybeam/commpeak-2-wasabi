"""Active Directory as a sign-in method, and accounts that can be created before
their first sign-in.

Two problems with the people page, both found by trying to use it:

**Active Directory was not an option at all.** `auth_source` allowed LOCAL,
ENTRA and GOOGLE. A domain controller is the one directory these customers
actually run, and the console can now search it -- but could not record that a
person signs in through it.

**A directory account could not be created.**
``ck_users_credentials_match_source`` demanded ``oidc_subject IS NOT NULL`` for
anything that is not LOCAL. That subject only exists *after* the person has
signed in once and the identity provider has told us who they are, so the form
could offer "Microsoft Entra ID" and then fail on save. Pre-provisioning -- add
the person now, let them sign in later -- is the normal way to onboard, and the
constraint forbade it.

The constraint still enforces the part that matters: a directory account never
holds a local password, and a local account always does. What it no longer does
is insist we know the person's directory id before they have ever appeared.

Revision ID: 0008
"""

from __future__ import annotations

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_credentials_match_source")
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_credentials_match_source CHECK (
            (auth_source = 'LOCAL'  AND password_hash IS NOT NULL)
         OR (auth_source <> 'LOCAL' AND password_hash IS NULL)
        )
        """
    )
    # LDAP is the value for a domain controller. Named for the protocol rather
    # than for "AD", because the same path serves any LDAP directory; the UI
    # says "Active Directory", which is what the customer calls it.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_auth_source")
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_auth_source CHECK (
            auth_source IN ('LOCAL', 'ENTRA', 'GOOGLE', 'LDAP')
        )
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_auth_source")
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_credentials_match_source")
    # Restoring the old rule would reject any pre-provisioned directory account
    # created in the meantime, so clear them rather than fail the downgrade.
    op.execute(
        "DELETE FROM users WHERE auth_source <> 'LOCAL' AND oidc_subject IS NULL"
    )
    op.execute("UPDATE users SET auth_source = 'ENTRA' WHERE auth_source = 'LDAP'")
    op.execute(
        """
        ALTER TABLE users ADD CONSTRAINT ck_users_credentials_match_source CHECK (
            (auth_source = 'LOCAL'  AND password_hash IS NOT NULL)
         OR (auth_source <> 'LOCAL' AND password_hash IS NULL
             AND oidc_subject IS NOT NULL)
        )
        """
    )
