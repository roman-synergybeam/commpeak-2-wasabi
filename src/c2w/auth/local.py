"""Local password authentication and session management.

Local accounts are the way in before Active Directory is connected, and
afterwards they remain as a break-glass super admin.  When Entra is enabled the
intent is to disable ``auth.local_accounts_enabled`` for everyone except
``SUPER_ADMIN`` -- :func:`authenticate` enforces exactly that, so switching to
AD does not require the login path to be rewritten.

Passwords use Argon2id with the library's current defaults.  Failed attempts
lock an account temporarily: these credentials guard access to recorded phone
calls, so an unlimited online guessing budget is not acceptable, and a lockout
that clears itself avoids creating a support burden.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.db.models.auth import AuthSource, Role, User, UserBrand, UserSession
from c2w.db.models.core import Brand
from c2w.settings import settings_service

__all__ = [
    "LOCKOUT_DURATION",
    "MAX_FAILED_LOGINS",
    "AuthError",
    "authenticate",
    "authenticate_directory",
    "create_session",
    "create_super_admin",
    "hash_password",
    "hash_recovery_secret",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "verify_password",
]

_hasher = PasswordHasher()

MAX_FAILED_LOGINS = 8
LOCKOUT_DURATION = timedelta(minutes=15)
MIN_PASSWORD_LENGTH = 12


class AuthError(Exception):
    """Authentication failed.

    The message is deliberately the same for a wrong password and an unknown
    account, so the login form cannot be used to enumerate valid users.
    """


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def hash_recovery_secret(secret: str) -> str:
    """Hash a two-factor recovery code.

    Separate from :func:`hash_password` because that enforces a minimum length
    written for something a person invented. A recovery code is generated, its
    entropy is known, and it is shorter than that policy allows -- running it
    through the password rule would reject the platform's own codes.
    """
    if not secret:
        raise AuthError("empty recovery code")
    return _hasher.hash(secret)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


async def authenticate_directory(
    session: AsyncSession, email: str, password: str
) -> User | None:
    """Sign somebody in against Active Directory. None if it is not their route.

    Returns None -- rather than raising -- when this is not a directory
    account, so the caller can fall through to a local password. Raises
    :class:`AuthError` only when it *is* a directory account and the directory
    refused, because at that point saying "wrong password" is correct.

    Group membership decides the role on every sign-in, not just the first:
    somebody removed from the admins group should stop being an admin the next
    time they sign in, not whenever a sync happens to run.
    """
    from c2w.auth import directory

    email = email.strip().lower()
    if not await settings_service.get_bool(session, "ldap.enabled"):
        return None

    config = await directory.load_config(session)
    if not config.configured:
        return None

    person = await directory.authenticate(config, email, password)
    if person is None:
        # Either not in the directory at all, or the directory refused. If we
        # hold a directory account for this address, the refusal is theirs to
        # hear; otherwise say nothing and let the local path answer.
        existing = (
            await session.execute(
                select(User).where(
                    User.email == email, User.auth_source == AuthSource.LDAP
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise AuthError("invalid email or password")
        return None

    groups = await directory.group_memberships(config, person.dn)
    admin_group = (await settings_service.get_str(session, "ldap.admin_group") or "").strip()
    user_group = (await settings_service.get_str(session, "ldap.user_group") or "").strip()

    role: Role | None = None
    if admin_group and admin_group in groups:
        role = Role.ADMIN
    elif user_group and user_group in groups:
        role = Role.OPERATOR
    elif not admin_group and not user_group:
        # No mapping configured: everyone the directory accepts gets the least
        # privilege, rather than nobody being able to sign in at all.
        role = Role.OPERATOR

    if role is None:
        raise AuthError(
            "your directory account is not in a group that has access to this system"
        )

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        # Pre-provisioned accounts are the normal path, but somebody in the
        # right group who was never added by hand should still get in.
        brands = (
            await session.execute(
                select(Brand.id).where(Brand.is_active).order_by(Brand.id).limit(1)
            )
        ).scalar_one_or_none()
        if brands is None:
            raise AuthError("this system has no organisation to place you in yet")
        user = User(
            brand_id=brands,
            email=email,
            display_name=person.name or email,
            role=role,
            auth_source=AuthSource.LDAP,
            password_hash=None,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        session.add(UserBrand(user_id=user.id, brand_id=brands, role=role))
    elif user.auth_source != AuthSource.LDAP:
        # A local account already owns this address. Silently converting it
        # would let anyone who can create a directory entry take it over.
        raise AuthError("this account signs in with a password kept here")

    if not user.is_active:
        raise AuthError("account is disabled")

    user.role = role
    user.oidc_subject = person.dn
    user.oidc_groups = sorted(groups)
    user.last_login_at = datetime.now(UTC)
    user.failed_logins = 0
    user.locked_until = None
    await session.flush()
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Verify credentials and return the user, or raise :class:`AuthError`."""
    user = (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()

    # Hash even when the user does not exist, so response time does not reveal
    # whether an address is registered.
    if user is None or not user.password_hash:
        _hasher.hash("timing-equalisation-only")
        raise AuthError("invalid email or password")

    if not user.is_active:
        raise AuthError("account is disabled")

    now = datetime.now(UTC)
    if user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() / 60) + 1
        raise AuthError(f"account is locked for another {remaining} minute(s)")

    if user.auth_source != AuthSource.LOCAL:
        raise AuthError("this account signs in through your identity provider")

    # Once AD is in place, local login stays available only to the break-glass
    # super admin.
    if not await settings_service.get_bool(session, "auth.local_accounts_enabled"):
        if user.role != Role.SUPER_ADMIN:
            raise AuthError("local sign-in is disabled; use your organisation account")

    if not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + LOCKOUT_DURATION
            user.failed_logins = 0
        await session.flush()
        raise AuthError("invalid email or password")

    if needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    await session.flush()
    return user


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, UserSession]:
    """Create a session and return ``(cookie_value, row)``.

    Only the hash is stored, so the database never holds a usable session token.
    """
    token = secrets.token_urlsafe(48)
    ttl = await settings_service.get_int(session, "core.session_ttl_seconds")
    row = UserSession(
        token_hash=_hash_token(token),
        user_id=user.id,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
        ip=ip,
        user_agent=user_agent,
    )
    session.add(row)
    await session.flush()
    return token, row


async def resolve_session(session: AsyncSession, token: str) -> User | None:
    """Return the user for a session cookie, or None if it is not usable.

    Checks the user's current state, not just the session's: a disabled account
    loses access immediately rather than when its session would have expired.
    """
    if not token:
        return None
    row = (
        await session.execute(
            select(UserSession).where(UserSession.token_hash == _hash_token(token))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at <= datetime.now(UTC):
        return None

    user = (await session.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    row.last_seen_at = datetime.now(UTC)
    return user


async def revoke_session(session: AsyncSession, token: str) -> None:
    await session.execute(
        update(UserSession)
        .where(UserSession.token_hash == _hash_token(token))
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_all_sessions(session: AsyncSession, user_id: int) -> None:
    """Revoke every session for a user -- on password change or deactivation."""
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )


async def create_super_admin(
    session: AsyncSession,
    email: str,
    password: str,
    *,
    display_name: str | None = None,
) -> User:
    """Create the platform super admin.

    Brand-less by design: this account administers every brand, which is why it
    is the one role RLS is bypassed for and why there should be very few of
    them.  Refuses to overwrite an existing account, so re-running the
    installer cannot silently reset a password.
    """
    email = email.strip().lower()
    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise AuthError(f"a user with email {email} already exists")

    user = User(
        brand_id=None,
        email=email,
        display_name=display_name or email,
        role=Role.SUPER_ADMIN,
        auth_source=AuthSource.LOCAL,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user
