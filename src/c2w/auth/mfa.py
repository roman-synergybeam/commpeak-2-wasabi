"""Enrolling and checking the second factor.

The state machine matters more than the arithmetic (which lives in
:mod:`c2w.auth.totp`), because the failure modes here lock people out of a
system some of them administer alone:

    no secret  --begin-->  pending  --confirm-->  active
                              |                     |
                              +------ cancel -------+

A **pending** enrolment is a secret that exists but has never been proved
against a real authenticator. It is never demanded at login. Skipping that
state -- switching the requirement on the moment a secret is generated -- means
anyone who closes the tab half way through can no longer sign in, and for the
platform administrator there may be no one above them to fix it.

Recovery codes are issued at confirmation rather than at ``begin``, so they are
never handed out for a secret that turned out not to work.

Between the password step and the code step the browser holds a short-lived
signed ticket, not a session. A session that exists before the second factor
has been checked is a session that can be used, which would make the whole step
decorative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.auth import totp
from c2w.auth.local import AuthError, revoke_all_sessions, verify_password
from c2w.config import get_bootstrap
from c2w.crypto import open_global, seal_global
from c2w.db.models.auth import AuthSource, Role, User
from c2w.settings import settings_service

__all__ = [
    "TICKET_MAX_AGE_SECONDS",
    "Enrolment",
    "begin_enrolment",
    "can_reset_for",
    "cancel_enrolment",
    "check_second_factor",
    "confirm_enrolment",
    "disable",
    "issue_ticket",
    "read_ticket",
    "second_factor_required",
]

#: Long enough to fetch a phone, short enough that an abandoned half-login on a
#: shared machine is not a way in.
TICKET_MAX_AGE_SECONDS = 300


def _aad(user_id: int) -> str:
    """Bind a sealed secret to its user.

    Without this, a sealed secret copied from one row to another would still
    unseal, and an attacker with write access to the table could replace an
    administrator's factor with one they hold.
    """
    return f"totp:{user_id}"


@dataclass(frozen=True)
class Enrolment:
    """What the enrolment page needs. The secret is shown once, here."""

    secret: str
    typed_secret: str
    uri: str
    qr_svg: str


async def _issuer(session: AsyncSession) -> str:
    name = await settings_service.get(session, "mfa.issuer_name")
    return str(name or "c2w").strip() or "c2w"


async def begin_enrolment(session: AsyncSession, user: User) -> Enrolment:
    """Generate a secret and return what the page must display.

    Replaces any secret that is still pending -- someone who reloads the page
    should get a working QR code, not the one from an authenticator they have
    already deleted. An **active** factor is not replaced silently; call
    :func:`disable` first, which asks for a password.
    """
    if user.auth_source != AuthSource.LOCAL:
        raise AuthError("this account's second factor is managed by your identity provider")
    if user.totp_active:
        raise AuthError("two-factor is already on for this account")

    secret = totp.new_secret()
    user.totp_secret_sealed = seal_global(secret, aad=_aad(user.id))
    user.totp_enrolled_at = None
    user.totp_last_counter = None
    await session.flush()

    issuer = await _issuer(session)
    uri = totp.provisioning_uri(secret, account=user.email, issuer=issuer)
    return Enrolment(
        secret=secret,
        typed_secret=totp.format_secret(secret),
        uri=uri,
        qr_svg=totp.qr_svg(uri),
    )


async def confirm_enrolment(session: AsyncSession, user: User, code: str) -> list[str]:
    """Prove the secret works, switch it on, and return the recovery codes.

    The codes are returned in plain text exactly once; only their hashes are
    stored, so a lost list cannot be recovered, only replaced.
    """
    if not user.totp_secret_sealed:
        raise AuthError("start the setup again -- there is no pending secret")
    if user.totp_enrolled_at is not None:
        raise AuthError("two-factor is already on for this account")

    secret = open_global(user.totp_secret_sealed, aad=_aad(user.id))
    counter = totp.verify(secret, code)
    if counter is None:
        raise AuthError("that code did not match -- check the clock on your phone")

    codes = totp.new_recovery_codes()
    user.totp_enrolled_at = datetime.now(UTC)
    user.totp_last_counter = counter
    user.totp_recovery_hashes = [totp.hash_recovery_code(c) for c in codes]
    await session.flush()
    return codes


async def cancel_enrolment(session: AsyncSession, user: User) -> None:
    """Throw away a secret that was never confirmed."""
    if user.totp_enrolled_at is not None:
        raise AuthError("two-factor is on; turn it off instead")
    user.totp_secret_sealed = None
    user.totp_last_counter = None
    await session.flush()


async def disable(session: AsyncSession, user: User, password: str) -> None:
    """Turn the second factor off, on proof of the password.

    The password is required because the cost of being wrong is asymmetric: an
    unattended session should not be able to remove the factor that protects
    the account.
    """
    if not user.password_hash or not verify_password(user.password_hash, password):
        raise AuthError("that password is not right")
    user.totp_secret_sealed = None
    user.totp_enrolled_at = None
    user.totp_last_counter = None
    user.totp_recovery_hashes = []
    await session.flush()


async def second_factor_required(session: AsyncSession, user: User) -> bool:
    """Whether this sign-in must present a code.

    An active factor is always checked, even if the requirement is off -- once
    somebody has turned it on for themselves, an administrator flipping a
    global switch should not quietly stop asking for it.

    The requirement setting only forces *enrolment*, and never applies to a
    federated account, whose directory already carries its own factor.
    """
    if user.totp_active:
        return True
    if user.auth_source != AuthSource.LOCAL:
        return False
    return await settings_service.get_bool(session, "mfa.require_totp")


async def check_second_factor(session: AsyncSession, user: User, entered: str) -> None:
    """Accept a TOTP code or a recovery code, or raise :class:`AuthError`.

    A used recovery code is removed here rather than by the caller, so a
    caller that forgets cannot leave it reusable.
    """
    if not user.totp_active or not user.totp_secret_sealed:
        raise AuthError("two-factor is not set up on this account")

    secret = open_global(user.totp_secret_sealed, aad=_aad(user.id))
    counter = totp.verify(secret, entered, last_counter=user.totp_last_counter)
    if counter is not None:
        user.totp_last_counter = counter
        await session.flush()
        return

    hashes = list(user.totp_recovery_hashes or [])
    used = totp.verify_recovery_code(hashes, entered)
    if used is not None:
        hashes.remove(used)
        user.totp_recovery_hashes = hashes
        # A recovery code means the authenticator is gone or unreachable. Every
        # other session becomes suspect at that point, so they all end.
        await revoke_all_sessions(session, user.id)
        await session.flush()
        return

    raise AuthError("that code is not right, or has already been used")


def _signer() -> TimestampSigner:
    key = get_bootstrap().master_key.get_secret_value()
    if not key:
        raise AuthError("the platform master key is not configured")
    # Salted so this signer's tickets cannot be swapped for any other value
    # signed with the same key.
    return TimestampSigner(key, salt="c2w-mfa-ticket")


def issue_ticket(user: User) -> str:
    """A signed half-login: this password was correct, the code was not seen yet."""
    return _signer().sign(str(user.id)).decode()


def read_ticket(ticket: str) -> int | None:
    """The user id from a ticket, or None if it is absent, stale or forged."""
    if not ticket:
        return None
    try:
        raw = _signer().unsign(ticket, max_age=TICKET_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired, AuthError):
        return None
    try:
        return int(raw.decode())
    except ValueError:  # pragma: no cover - a signed non-integer cannot occur
        return None


def can_reset_for(actor: User, target: User) -> bool:
    """Whether ``actor`` may clear ``target``'s second factor.

    Someone has to be able to, or a lost phone is a lost account. Only a
    platform administrator can, and not on themselves -- resetting your own
    factor from a live session would make it optional.
    """
    if actor.role != Role.SUPER_ADMIN:
        return False
    return actor.id != target.id
