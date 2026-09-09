"""Brand isolation is a database guarantee, so it is tested against a database.

These tests need a real PostgreSQL with the migration applied, because the thing
under test is a set of RLS policies -- there is nothing meaningful to assert
against a mock.  They are skipped unless ``C2W_TEST_DATABASE_URL`` points at a
scratch database.

    createdb c2w_test
    C2W_DATABASE_URL=postgresql+asyncpg://.../c2w_test uv run alembic upgrade head
    C2W_TEST_DATABASE_URL=postgresql+asyncpg://.../c2w_test \
        uv run pytest tests/test_brand_isolation.py

The connecting role must NOT be a superuser and must NOT hold BYPASSRLS;
either one silently bypasses every policy and would make these tests pass
while proving nothing.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)

# Two fixed brand ids, which keeps each test's intent readable. The names are
# deliberately generic: what is under test is the isolation boundary, not the
# customers.
GO4REX, INTERMAGNUM = 1, 2


@pytest.fixture
async def sessions():
    engine = create_async_engine(TEST_DB, poolclass=None)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        role = (await s.execute(text("SELECT current_user"))).scalar_one()
        flags = (
            await s.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert not flags[0] and not flags[1], (
            f"role {role!r} is superuser/BYPASSRLS; RLS would be bypassed and these "
            "tests would pass without proving anything"
        )

        # Slugs are namespaced to this module so the fixture cannot collide with
        # brands created by other tests or by hand, and ON CONFLICT is untargeted
        # so it tolerates either the id or the slug already existing.
        await s.execute(
            text(
                "INSERT INTO brands (id, name, slug) VALUES "
                "(:g,'Isolation A','iso-brand-a'), (:i,'Isolation B','iso-brand-b') "
                "ON CONFLICT DO NOTHING"
            ),
            {"g": GO4REX, "i": INTERMAGNUM},
        )
        for brand in (GO4REX, INTERMAGNUM):
            for table in ("cdrs", "recordings"):
                await s.execute(
                    text(
                        f"CREATE TABLE IF NOT EXISTS {table}_brand_{brand} "
                        f"PARTITION OF {table} FOR VALUES IN ({brand})"
                    )
                )
        # Inserting an explicit id does not advance the sequence, so the next
        # caller that lets the sequence assign one gets a duplicate primary
        # key. That shows up only against a fresh database, in whichever test
        # happens to run next -- which reads as a flake somewhere unrelated.
        await s.execute(
            text("SELECT setval('brands_id_seq', GREATEST(:n, (SELECT max(id) FROM brands)))"),
            {"n": max(GO4REX, INTERMAGNUM)},
        )
        await s.commit()
    yield factory
    await engine.dispose()


async def _scoped(factory, brand_id: int | None):
    session = factory()
    if brand_id is not None:
        await session.execute(
            text("SELECT set_config('c2w.brand_id', :b, true)"), {"b": str(brand_id)}
        )
    return session


async def _seed_call(session, brand_id: int, call_uuid: str) -> None:
    await session.execute(
        text(
            "INSERT INTO cdrs (brand_id, connection_id, tenant_id, call_uuid, start_at) "
            "VALUES (:b, 1, 1, :u, now()) ON CONFLICT DO NOTHING"
        ),
        {"b": brand_id, "u": call_uuid},
    )


async def test_each_brand_sees_only_its_own_calls(sessions):
    async with await _scoped(sessions, GO4REX) as s:
        await _seed_call(s, GO4REX, "iso-go4rex")
        await s.commit()
    async with await _scoped(sessions, INTERMAGNUM) as s:
        await _seed_call(s, INTERMAGNUM, "iso-intermagnum")
        await s.commit()

    async with await _scoped(sessions, GO4REX) as s:
        rows = (await s.execute(text("SELECT call_uuid FROM cdrs"))).scalars().all()
        assert "iso-go4rex" in rows
        assert "iso-intermagnum" not in rows, "Go4Rex must never see InterMagnum's calls"

    async with await _scoped(sessions, INTERMAGNUM) as s:
        rows = (await s.execute(text("SELECT call_uuid FROM cdrs"))).scalars().all()
        assert "iso-intermagnum" in rows
        assert "iso-go4rex" not in rows


async def test_targeted_read_of_other_brand_returns_nothing(sessions):
    """Even naming the other brand's row explicitly must find nothing."""
    async with await _scoped(sessions, INTERMAGNUM) as s:
        await _seed_call(s, INTERMAGNUM, "iso-target")
        await s.commit()
    async with await _scoped(sessions, GO4REX) as s:
        count = (
            await s.execute(
                text("SELECT count(*) FROM cdrs WHERE call_uuid = 'iso-target'")
            )
        ).scalar_one()
    assert count == 0


async def test_unscoped_session_sees_nothing_and_does_not_error(sessions):
    """A request that forgot to scope must fail closed, cleanly.

    The policy uses NULLIF(current_setting(...), '') precisely so a pooled
    connection whose variable was reset yields no rows rather than raising
    ``invalid input syntax for type bigint``.
    """
    async with await _scoped(sessions, None) as s:
        await s.execute(text("RESET c2w.brand_id"))
        count = (await s.execute(text("SELECT count(*) FROM cdrs"))).scalar_one()
    assert count == 0


async def test_cross_brand_insert_is_rejected(sessions):
    """WITH CHECK must stop a brand writing into another brand's partition."""
    from sqlalchemy.exc import DBAPIError

    async with await _scoped(sessions, GO4REX) as s:
        with pytest.raises(DBAPIError, match="row-level security"):
            await _seed_call(s, INTERMAGNUM, "iso-smuggled")
            await s.flush()
        await s.rollback()


async def test_cross_brand_update_affects_no_rows(sessions):
    async with await _scoped(sessions, INTERMAGNUM) as s:
        await _seed_call(s, INTERMAGNUM, "iso-immutable")
        await s.commit()
    async with await _scoped(sessions, GO4REX) as s:
        result = await s.execute(
            text("UPDATE cdrs SET call_uuid = 'hijacked' WHERE call_uuid = 'iso-immutable'")
        )
        assert result.rowcount == 0
        await s.rollback()


async def test_audit_events_are_append_only(sessions):
    """An audit trail that can be edited is not an audit trail.

    The label is unique per run: the table is append-only by design, so rows
    from previous runs are still there and a fixed label would make this test
    fail for the wrong reason.
    """
    import uuid

    label = f"isolation-test-{uuid.uuid4().hex[:8]}"

    async with await _scoped(sessions, GO4REX) as s:
        await s.execute(
            text(
                "INSERT INTO audit_events (brand_id, action, result, actor_label) "
                "VALUES (:b, 'PLAY', 'SUCCESS', :label)"
            ),
            {"b": GO4REX, "label": label},
        )
        await s.commit()

    async with await _scoped(sessions, GO4REX) as s:
        updated = await s.execute(
            text("UPDATE audit_events SET result = 'TAMPERED' WHERE actor_label = :label"),
            {"label": label},
        )
        deleted = await s.execute(
            text("DELETE FROM audit_events WHERE actor_label = :label"), {"label": label}
        )
        await s.commit()
        assert updated.rowcount == 0
        assert deleted.rowcount == 0

    # Re-scope before reading: set_config(..., true) is SET LOCAL, so the brand
    # scope ended with the commit above.  That transaction-bound lifetime is
    # deliberate -- it is what stops a scope leaking to the next request that
    # borrows this pooled connection -- so each transaction must set it again.
    async with await _scoped(sessions, GO4REX) as s:
        surviving = (
            (
                await s.execute(
                    text("SELECT result FROM audit_events WHERE actor_label = :label"),
                    {"label": label},
                )
            )
            .scalars()
            .all()
        )
        assert surviving == ["SUCCESS"], "the original audit row must survive tamper attempts"


async def test_partitions_route_rows_to_the_right_brand(sessions):
    """Rows must land in their brand's partition -- that is the isolation story
    as much as it is the performance story."""
    async with await _scoped(sessions, GO4REX) as s:
        await _seed_call(s, GO4REX, "iso-partition")
        await s.commit()
    async with await _scoped(sessions, GO4REX) as s:
        located = (
            await s.execute(
                text("SELECT count(*) FROM cdrs_brand_1 WHERE call_uuid = 'iso-partition'")
            )
        ).scalar_one()
        assert located == 1
