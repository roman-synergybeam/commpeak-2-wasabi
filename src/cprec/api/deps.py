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

from cprec.auth.local import resolve_session
from cprec.auth.rbac import Permission, has_permission
from cprec.db.models.auth import User
from cprec.db.models.core import Brand
from cprec.db.session import get_sessionmaker

SESSION_COOKIE = "cprec_session"
BRAND_COOKIE = "cprec_brand"

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
    cprec_session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User | None:
    if not cprec_session:
        return None
    return await resolve_session(session, cprec_session)


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
    cprec_brand: Annotated[str | None, Cookie(alias=BRAND_COOKIE)] = None,
) -> int:
    """Which brand this request operates on.

    An ordinary user is pinned to their own brand and the cookie is ignored --
    it is user input, and this is an isolation boundary.  A super admin spans
    every brand, so they get the one they selected, or the first available if
    they have not chosen yet.
    """
    if not user.is_super_admin:
        if user.brand_id is None:
            # The schema forbids this, so reaching it means something is wrong
            # rather than merely unconfigured.
            raise HTTPException(status.HTTP_403_FORBIDDEN, "your account has no organisation")
        return user.brand_id

    brands = await selectable_brands(session, user)
    if not brands:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no organisation exists yet -- create one with: "
            "cprec-admin brand add --name '<name>' --slug <slug>",
        )
    if cprec_brand and cprec_brand.isdigit():
        chosen = int(cprec_brand)
        if any(b.id == chosen for b in brands):
            return chosen
    # No valid selection: fall back to the first rather than refusing to render.
    return brands[0].id


async def scoped_session(
    brand_id: Annotated[int, Depends(active_brand_id)],
) -> AsyncIterator[AsyncSession]:
    """A session restricted to the active brand by RLS."""
    from cprec.db.session import brand_session

    async with brand_session(brand_id) as session:
        yield session


ScopedSession = Annotated[AsyncSession, Depends(scoped_session)]


def require(permission: Permission):
    """Dependency factory gating a route on one permission."""

    async def guard(user: CurrentUser) -> User:
        if not has_permission(user.role, permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"your role ({user.role}) does not allow {permission}",
            )
        return user

    return Depends(guard)


async def selectable_brands(session: AsyncSession, user: User) -> list[Brand]:
    """Brands this user may act on, for the switcher in the UI."""
    stmt = select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.name)
    if not user.is_super_admin:
        stmt = stmt.where(Brand.id == user.brand_id)
    return list((await session.execute(stmt)).scalars().all())
