"""The append-only trail for administrative actions.

Media access has been audited since the beginning -- who played what, who
downloaded it, who was refused. Administration was not: creating an account,
disabling one, clearing somebody's second factor and changing a password all
went to the structured log and nowhere else.

That is the wrong half to leave out. A log file is rotated, is writable by
whoever can reach the disk, and is not what anybody consults when asking "who
gave this person access". ``audit_events`` has `UPDATE` and `DELETE` revoked
precisely so it can answer that, and these are the events it most needs.

Every action here is recorded with the actor, not just the target: "the account
was disabled" is not the useful fact.
"""

from __future__ import annotations

import enum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.db.models.auth import User
from c2w.db.models.core import AuditEvent
from c2w.logging import get_logger

__all__ = ["AdminAction", "record_admin_event"]

log = get_logger(__name__)


class AdminAction(enum.StrEnum):
    """Administrative events worth keeping for ever.

    Deliberately coarse: one value per thing a person did, so the audit page's
    action filter stays readable. The specifics go in ``detail``.
    """

    ORGANISATION_CREATED = "ORGANISATION_CREATED"
    ORGANISATION_RENAMED = "ORGANISATION_RENAMED"
    TENANT_CREATED = "TENANT_CREATED"
    USER_CREATED = "USER_CREATED"
    USER_ENABLED = "USER_ENABLED"
    USER_DELETED = "USER_DELETED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_DETAILS_CHANGED = "USER_DETAILS_CHANGED"
    PASSWORD_RESET_BY_ADMIN = "PASSWORD_RESET_BY_ADMIN"  # noqa: S105 - an action name
    USER_BRAND_ADDED = "USER_BRAND_ADDED"
    USER_BRAND_REMOVED = "USER_BRAND_REMOVED"
    USER_DISABLED = "USER_DISABLED"
    #: An administrator cleared somebody else's authenticator after a lost
    #: phone. The single most abusable action in the system, since it removes a
    #: factor from an account the actor does not own.
    MFA_RESET_BY_ADMIN = "MFA_RESET_BY_ADMIN"
    MFA_ENABLED = "MFA_ENABLED"
    MFA_DISABLED = "MFA_DISABLED"
    RECOVERY_CODES_REISSUED = "RECOVERY_CODES_REISSUED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105 - an action name, not a credential


async def record_admin_event(
    session: AsyncSession,
    *,
    actor: User,
    action: AdminAction,
    brand_id: int | None,
    target: User | None = None,
    # "SUCCESS"/"DENIED", matching what media access already writes. A third
    # vocabulary here would make the audit page's own filter wrong: it treats
    # anything that is not SUCCESS as a refusal.
    result: str = "SUCCESS",
    ip: str | None = None,
    user_agent: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one administrative event.

    ``brand_id`` has to be the scope the request is running under, because
    ``audit_events`` carries the same forced RLS as everything else and its
    ``WITH CHECK`` rejects a row for any other brand. Passing the *target's*
    brand would therefore fail for a platform administrator acting from a
    different organisation -- and a platform administrator has no brand of
    their own, which is exactly when this matters.
    """
    if brand_id is None:
        # `audit_events` carries the same forced RLS as everything else, and
        # the policy compares `brand_id` to the session's scope -- so a row
        # with no brand cannot satisfy it (NULL = NULL is not true). A
        # platform-level action with no organisation in scope therefore cannot
        # be recorded here. That only arises before any organisation exists,
        # when the CLI is doing the work; it is logged loudly rather than
        # silently dropped, and rather than failing the request.
        log.warning(
            "audit.unscoped_admin_event",
            action=str(action),
            actor=actor.email,
            target=getattr(target, "email", None),
        )
        return

    body = dict(detail or {})
    if target is not None:
        # The target's identity goes in detail rather than in a column: there
        # is no target_user_id, and adding one would need a migration for
        # something a JSONB field already records adequately.
        body.setdefault("target_user_id", target.id)
        body.setdefault("target_email", target.email)
        body.setdefault("target_role", str(target.role))

    # The administrative routes run on an unscoped session on purpose -- they
    # work across organisations -- so the scope is set here for the insert and
    # put back afterwards. Set, rather than left to the caller, because an
    # audit row that fails to write is the one outcome this function must not
    # have; restored, because silently repointing a caller's brand scope is
    # how a later query ends up reading the wrong organisation's data.
    previous = (
        await session.execute(text("SELECT current_setting('c2w.brand_id', true)"))
    ).scalar_one()
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
    )
    try:
        session.add(
            AuditEvent(
                brand_id=brand_id,
                actor_user_id=actor.id,
                actor_label=actor.email,
                ip=ip,
                user_agent=user_agent,
                action=str(action),
                result=result,
                detail=body,
            )
        )
        await session.flush()
    finally:
        await session.execute(
            text("SELECT set_config('c2w.brand_id', :b, true)"),
            {"b": previous or ""},
        )
