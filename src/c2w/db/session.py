"""Engine, session factory and the brand-scoping helper.

Every database session that serves a user request must be scoped to exactly one
brand.  ``brand_session`` does that by setting the ``c2w.brand_id`` session
variable that the Row-Level Security policies read.

The scoping is set with ``SET LOCAL``, so it is bound to the transaction and
cannot leak to the next request that borrows the same pooled connection -- a
leak there would mean one company seeing another's calls, which is the single
worst failure this system could have.

Workers and the reconciler legitimately span brands.  They connect as a role
holding ``BYPASSRLS`` (``platform_session``) rather than disabling policies in
SQL, so the privilege is visible in ``pg_roles`` and auditable.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.sql import text

from c2w.config import Bootstrap, get_bootstrap

__all__ = [
    "MissingBrandScope",
    "brand_session",
    "configure_pool",
    "dispose_engine",
    "get_engine",
    "get_platform_engine",
    "get_platform_sessionmaker",
    "get_sessionmaker",
    "platform_session",
    "pool_capacity",
]

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_platform_engine: AsyncEngine | None = None
_platform_sessionmaker: async_sessionmaker[AsyncSession] | None = None

#: Pool sizing is a bootstrap concern, not a database setting: the pool is built
#: before any setting can be read. These are sized for one modest VM running an
#: API plus a handful of workers.
POOL_SIZE = 10
MAX_OVERFLOW = 10

#: Per-process pool limits, by role. PostgreSQL allows 100 connections and
#: keeps 3 of them for superusers; at 10 + 10 everywhere, five workers alone
#: could open all 100, and with a real queue to work through they did: the
#: workers and the console were refused with "remaining connection slots are
#: reserved". The roles add up to 90 -- 5 x 12 for workers, 20 for the API,
#: 5 each for the scheduler and transcriber -- leaving room for a maintenance
#: session. Change one and re-add the sum.
WORKER_POOL = (6, 6)
BACKGROUND_POOL = (3, 2)

_pool_limits: tuple[int, int] = (POOL_SIZE, MAX_OVERFLOW)


def configure_pool(pool_size: int, max_overflow: int) -> None:
    """Size this process's connection pools. Call before the first session."""
    global _pool_limits
    if _engine is not None or _platform_engine is not None:
        raise RuntimeError("configure_pool() called after an engine was built")
    _pool_limits = (pool_size, max_overflow)


def pool_capacity() -> int:
    """The most connections one of this process's pools will open."""
    return sum(_pool_limits)
STATEMENT_TIMEOUT_MS = 30_000
#: A transaction left open with nothing running in it is ended after this.
#: Only a stuck task does that -- a transcription hung for three and a half
#: days once held its snapshot the whole time, which slowed every full scan in
#: the database. Generous, because ending a merely slow one is not free either.
IDLE_IN_TRANSACTION_TIMEOUT_MS = 3_600_000


class MissingBrandScope(RuntimeError):
    """Raised when a brand-scoped session is requested without a brand."""


def _build_engine(url: str, app_name: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=_pool_limits[0],
        max_overflow=_pool_limits[1],
        pool_pre_ping=True,
        echo=False,
        connect_args={
            "server_settings": {
                "application_name": app_name,
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": str(
                    IDLE_IN_TRANSACTION_TIMEOUT_MS
                ),
            }
        },
    )


def get_engine(bootstrap: Bootstrap | None = None) -> AsyncEngine:
    """Engine for the brand-scoped application role (RLS applies)."""
    global _engine
    if _engine is None:
        cfg = bootstrap or get_bootstrap()
        _engine = _build_engine(str(cfg.database_url), "c2w")
    return _engine


def get_platform_engine(bootstrap: Bootstrap | None = None) -> AsyncEngine:
    """Engine for the cross-brand platform role (BYPASSRLS)."""
    global _platform_engine
    if _platform_engine is None:
        cfg = bootstrap or get_bootstrap()
        _platform_engine = _build_engine(cfg.effective_platform_url, "c2w-platform")
    return _platform_engine


def get_sessionmaker(bootstrap: Bootstrap | None = None) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(bootstrap), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


def get_platform_sessionmaker(
    bootstrap: Bootstrap | None = None,
) -> async_sessionmaker[AsyncSession]:
    global _platform_sessionmaker
    if _platform_sessionmaker is None:
        _platform_sessionmaker = async_sessionmaker(
            get_platform_engine(bootstrap), expire_on_commit=False, autoflush=False
        )
    return _platform_sessionmaker


async def dispose_engine() -> None:
    """Close both pools.  Call on application shutdown."""
    global _engine, _sessionmaker, _platform_engine, _platform_sessionmaker
    for engine in (_engine, _platform_engine):
        if engine is not None:
            await engine.dispose()
    _engine = _sessionmaker = _platform_engine = _platform_sessionmaker = None


@contextlib.asynccontextmanager
async def brand_session(brand_id: int | None) -> AsyncIterator[AsyncSession]:
    """Session restricted to one brand's rows by RLS.

    Refuses to open without a brand rather than defaulting to "all brands":
    a missing scope is a bug, and the safe response to a bug in an isolation
    boundary is to fail loudly.
    """
    if brand_id is None:
        raise MissingBrandScope(
            "brand_session requires a brand_id; use platform_session for cross-brand work"
        )
    factory = get_sessionmaker()
    async with factory() as session:
        await session.execute(
            text("SELECT set_config('c2w.brand_id', :brand, true)"),
            {"brand": str(int(brand_id))},
        )
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@contextlib.asynccontextmanager
async def platform_session() -> AsyncIterator[AsyncSession]:
    """Unscoped session for workers, the scheduler and the reconciler.

    Requires the connecting role to hold BYPASSRLS.  Never use this to serve a
    user request.
    """
    factory = get_platform_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
