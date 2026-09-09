"""Request dependencies: sessions, the current user, and brand scoping.

Two rules are enforced here so no individual route has to remember them:

*Every request gets a brand-scoped database session.*  The scope comes from the
signed-in user, and RLS does the rest.  A route cannot accidentally query
across brands because the session it is handed cannot see other brands' rows.

*A super admin has to choose a brand.*  They span every brand, so there is no
implicit scope for them; the chosen brand rides in a cookie and can be switched.
Without that, "current brand" would be ambiguous exactly for the account with
the most access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from c2w.auth.local import resolve_session
from c2w.auth.rbac import Permission, has_permission
from c2w.db.models.auth import Role, User, UserBrand
from c2w.db.models.core import Brand
from c2w.db.session import get_sessionmaker

SESSION_COOKIE = "c2w_session"
BRAND_COOKIE = "c2w_brand"

__all__ = [
    "BRAND_COOKIE",
    "SESSION_COOKIE",
    "CurrentUser",
    "ScopedSession",
    "client_ip",
    "current_user",
    "get_session",
    "optional_user",
    "require",
    "scoped_session",
]


async def get_session() -> AsyncIterator[AsyncSession]:
    """An unscoped session, used only to resolve who is calling."""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


def client_ip(request: Request) -> str | None:
    """The caller's address, trusting nginx's X-Forwarded-For.

    Recorded on every media access, so it needs to be the real client rather
    than the loopback address of the proxy.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def optional_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    c2w_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User | None:
    if not c2w_session:
        return None
    return await resolve_session(session, c2w_session)


async def current_user(
    user: Annotated[User | None, Depends(optional_user)],
) -> User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in to continue")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def active_brand_id(
    user: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
    c2w_brand: Annotated[str | None, Cookie(alias=BRAND_COOKIE)] = None,
) -> int:
    """Which brand this request operates on.

    An ordinary user is pinned to their own brand and the cookie is ignored --
    it is user input, and this is an isolation boundary.  A super admin spans
    every brand, so they get the one they selected, or the first available if
    they have not chosen yet.
    """
    brands = await selectable_brands(session, user)
    if not user.is_super_admin:
        if not brands:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "your account has no organisation"
            )
        # The cookie is user input on an isolation boundary, so it is only
        # honoured when it names an organisation this person actually has.
        if c2w_brand and c2w_brand.isdigit():
            chosen = int(c2w_brand)
            if any(b.id == chosen for b in brands):
                return chosen
        return user.brand_id or brands[0].id

    if not brands:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no organisation exists yet -- create one with: "
            "c2w-admin brand add --name '<name>' --slug <slug>",
        )
    if c2w_brand and c2w_brand.isdigit():
        chosen = int(c2w_brand)
        if any(b.id == chosen for b in brands):
            return chosen
    # No valid selection: fall back to the first rather than refusing to render.
    return brands[0].id


async def scoped_session(
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> AsyncIterator[AsyncSession]:
    """A session restricted to the active brand by RLS."""
    from c2w.db.session import brand_session

    async with brand_session(brand_id) as session:
        yield session


ScopedSession = Annotated[AsyncSession, Depends(scoped_session)]


def require(permission: Permission):
    """Dependency factory gating a route on one permission.

    Checks the role for the organisation being acted on, not the one on the
    account: the same person can be an admin in one and an operator in another.
    """

    async def guard(
        user: CurrentUser,
        session: Annotated[AsyncSession, Depends(get_session)],
        brand_id: Annotated[int, Depends(active_brand_id)],
    ) -> User:
        role = await effective_role(session, user, brand_id)
        if not has_permission(role, permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"your role here ({role}) does not allow {permission}",
            )
        return user

    return Depends(guard)


async def selectable_brands(session: AsyncSession, user: User) -> list[Brand]:
    """Organisations this person may act in.

    A platform admin reaches every one by role. Everyone else reaches the ones
    listed against them, which is usually one and is sometimes several -- the
    same operator handles calls for more than one of these companies.
    """
    stmt = select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name)
    if user.is_super_admin:
        return list((await session.execute(stmt)).scalars().all())

    allowed = set(
        (
            await session.execute(
                select(UserBrand.brand_id).where(UserBrand.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    if user.brand_id:
        allowed.add(user.brand_id)
    if not allowed:
        return []
    return list(
        (await session.execute(stmt.where(Brand.id.in_(allowed)))).scalars().all()
    )


async def effective_role(session: AsyncSession, user: User, brand_id: int | None) -> Role:
    """What this person may do *in this organisation*.

    The role sits on the pairing, not on the person, so the answer depends on
    which organisation is being looked at: an admin for one of these companies
    may be an operator for the other.
    """
    if user.is_super_admin:
        return Role.SUPER_ADMIN
    if brand_id is not None:
        assigned = (
            await session.execute(
                select(UserBrand.role).where(
                    UserBrand.user_id == user.id, UserBrand.brand_id == brand_id
                )
            )
        ).scalar_one_or_none()
        if assigned is not None:
            return Role(assigned)
    # Falls back to the role on the account for their home organisation, which
    # is what an account created before the list existed relies on.
    return Role(user.role)
