"""How much disk the index actually occupies, per organisation and in total.

The reason this is worth reporting rather than looking up when somebody
wonders: the local index has to be *complete* before a single recording is
deleted at the source, so it only ever grows, and the question "will this host
hold it" has a deadline attached. A figure in every summary makes the growth
rate visible without anybody going to look.

Two figures, and they are not the same question:

* **Per organisation.** `cdrs`, `recordings` and `sms_messages` are
  LIST-partitioned by brand, so this is exact rather than apportioned, and it
  is a company's own figure -- which is the only one that belongs in that
  company's channel.
* **The whole database.** More than the sum of the organisations: the transfer
  queue, the change log and every unpartitioned table are in it too, and on
  this estate they come to about a fifth of the total. This is the platform
  reader's figure and deliberately does not go to an organisation, since it
  says roughly how much data the other company holds.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["as_gb", "brand_bytes", "database_bytes"]

_DATABASE = "SELECT pg_database_size(current_database())"

#: Partitions are found by their bound, not by their name.
#:
#: `recordings_brand_2` is this codebase's naming convention, and a partition
#: created by hand would not follow it, whereas the bound is what actually
#: decides which rows land there. The bound *text* is normalised to digits
#: because PostgreSQL renders a bigint bound quoted -- `FOR VALUES IN ('2')` --
#: and that rendering is not a documented contract. Comparing the literal
#: string matched nothing and reported 0.00 GB for every organisation, which is
#: the failure mode this comment exists to prevent a second time.
#:
#: One value per partition is assumed, which is what every partition here has;
#: a multi-value list would concatenate its digits and match neither brand.
_BRAND = """
SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)
FROM pg_class c
JOIN pg_inherits i ON i.inhrelid = c.oid
JOIN pg_class p ON p.oid = i.inhparent
WHERE p.relname IN ('cdrs', 'recordings', 'sms_messages')
  AND regexp_replace(pg_get_expr(c.relpartbound, c.oid), '[^0-9]', '', 'g') = :brand
"""


async def database_bytes(session: AsyncSession) -> int:
    """Total size of the database, indexes and all."""
    return int((await session.execute(text(_DATABASE))).scalar_one())


async def brand_bytes(session: AsyncSession, brand_id: int) -> int:
    """Size of one organisation's partitions, indexes and TOAST included."""
    return int(
        (await session.execute(text(_BRAND), {"brand": str(brand_id)})).scalar_one()
    )


def as_gb(byte_count: int) -> str:
    """A size for a chat message: GB to two places, with the unit attached.

    Every figure carries a unit -- the console kit's rule, and a bare "6.62" in
    a Telegram message is worse than in a table, where a column heading can
    carry it.
    """
    return f"{byte_count / 1e9:.2f} GB"
