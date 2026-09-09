"""Users, roles and sessions.

Local accounts exist so the platform can be administered from the first minute,
before any identity provider is wired up.  The intended end state is that
authentication comes from Active Directory / Entra ID and local accounts are
reduced to a break-glass super admin, so:

* a user is either local (``password_hash`` set) or federated
  (``oidc_issuer``/``oidc_subject`` set), never judged by which is populated at
  read time -- ``auth_source`` records it explicitly;
* roles are stored as data, and the Entra group -> role mapping writes into the
  same table, so switching to AD changes how a user is authenticated and which
  role they are assigned, not how permissions are evaluated;
* ``SUPER_ADMIN`` is the only role that is not brand-scoped.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from c2w.db.base import Base, IdMixin, TimestampMixin


class AuthSource(enum.StrEnum):
    LOCAL = "LOCAL"
    ENTRA = "ENTRA"
    GOOGLE = "GOOGLE"
    #: A domain controller. Named for the protocol, because the same path
    #: serves any LDAP directory; the console says "Active Directory", which
    #: is what customers call it.
    LDAP = "LDAP"


class Role(enum.StrEnum):
    """Three roles, most privileged first.

    Kept as an enum rather than a table because the permission sets are part of
    the product's security model, not customer configuration -- a customer
    inventing a role that can delete recordings should not be a data change.

    It was briefly seven. Nobody asked for seven, and a role nobody can describe
    in a sentence is one that gets handed out by guesswork:

    * ``SUPER_ADMIN`` runs the platform and spans every organisation.
    * ``ADMIN`` runs one organisation, and is the only role there that can
      delete a recording.
    * ``OPERATOR`` does the day job -- search calls, listen, export.
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"

    @property
    def label(self) -> str:
        return {"SUPER_ADMIN": "platform admin", "ADMIN": "admin",
                "OPERATOR": "operator"}[self.value]


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_brand", "brand_id"),
        Index("ix_users_oidc", "oidc_issuer", "oidc_subject", unique=True),
    )

    #: The organisation this person lands in. Their full access is the
    #: user_brands list; NULL only for SUPER_ADMIN, who spans every one.
    brand_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("brands.id", ondelete="CASCADE")
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(String(32), nullable=False)
    auth_source: Mapped[AuthSource] = mapped_column(
        String(16), nullable=False, server_default=text("'LOCAL'")
    )

    #: Argon2id. NULL for federated users, who have no local password at all.
    password_hash: Mapped[str | None] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    oidc_issuer: Mapped[str | None] = mapped_column(String(255))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    #: Groups as asserted by the IdP on last login, for troubleshooting a
    #: mapping that did not produce the expected role.
    oidc_groups: Mapped[list | None] = mapped_column(JSONB)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Where to reach this person. Columns rather than preference keys: these
    #: are addresses the platform sends to, not choices the person made, and
    #: the alert senders will read them when routing becomes per-person.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64))
    slack_user_id: Mapped[str | None] = mapped_column(String(64))

    #: What this person chose for themselves -- theme, text size, playback
    #: volume. Kept on the account so it follows them to another machine.
    preferences: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    failed_logins: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Authenticator-app second factor. The secret is sealed under the master
    #: key rather than a brand data key, because the platform administrator
    #: belongs to no brand. See :mod:`c2w.auth.totp`.
    totp_secret_sealed: Mapped[str | None] = mapped_column(Text)
    #: NULL means a secret has been generated but never confirmed with a
    #: working code. An unconfirmed secret must never be demanded at login, or
    #: an abandoned enrolment locks the account out.
    totp_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Newest time step already accepted, so one code cannot be used twice.
    totp_last_counter: Mapped[int | None] = mapped_column(BigInteger)
    #: Argon2 hashes of single-use recovery codes, consumed as they are used.
    totp_recovery_hashes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == Role.SUPER_ADMIN

    @property
    def totp_active(self) -> bool:
        """Enrolled *and* confirmed -- the only state that may be enforced."""
        return bool(self.totp_secret_sealed) and self.totp_enrolled_at is not None


class UserBrand(Base):
    """Which organisations a person works in, and as what.

    The role sits on the pairing rather than on the person: the same operator
    may be an admin for one of these companies and an operator for another, and
    a single ``users.role`` cannot say that.

    A platform admin has no rows here and needs none -- they reach every
    organisation by role.
    """

    __tablename__ = "user_brands"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    brand_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("brands.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[Role] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class UserSession(Base, IdMixin):
    """Server-side session.

    Sessions are rows rather than self-contained signed cookies so that
    disabling a user, or an administrator revoking access, takes effect on the
    next request instead of whenever a token happens to expire.  For a system
    holding call recordings, immediate revocation is worth the lookup.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user", "user_id"),
        Index("ix_user_sessions_expires", "expires_at"),
    )

    #: SHA-256 of the cookie value. The cookie itself is never stored, so a
    #: database leak does not hand over live sessions.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
