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


class Role(enum.StrEnum):
    """Roles, most privileged first.

    Kept as an enum rather than a table because the permission sets are part of
    the product's security model, not customer configuration -- a customer
    inventing a role with ``recordings.delete`` should not be a data change.
    """

    SUPER_ADMIN = "SUPER_ADMIN"
    TENANT_ADMIN = "TENANT_ADMIN"
    RECORDING_ADMIN = "RECORDING_ADMIN"
    SUPERVISOR = "SUPERVISOR"
    AGENT = "AGENT"
    AUDITOR = "AUDITOR"
    READ_ONLY = "READ_ONLY"


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_brand", "brand_id"),
        Index("ix_users_oidc", "oidc_issuer", "oidc_subject", unique=True),
    )

    #: NULL only for SUPER_ADMIN, who spans every brand.
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
    failed_logins: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_super_admin(self) -> bool:
        return self.role == Role.SUPER_ADMIN


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
