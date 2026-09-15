"""Proving the archive holds what CommPeak holds, before anything is deleted.

Deleting at the source is the one irreversible action this system can take, so
it needs evidence rather than a progress bar. The distinction this module
exists to make: **"we believe it is archived" and "it is archived" are not the
same claim**, and only the second justifies a delete.

Three numbers per account, and they are three different questions:

* **At source** -- what CommPeak actually lists. Only an inventory scan knows
  this, and only for the days it has walked, so the report says how far that
  is rather than implying it covers everything.
* **Indexed** -- rows in `recordings`. Exact, and a claim about our database
  rather than about CommPeak.
* **Verified** -- `verified_at` set, meaning size and SHA-256 agreed and the
  destination object answered a HEAD at the time. Exact, and still historical:
  it says the copy was good *then*.

That last gap is why `recheck_destination` exists. A verification from three
weeks ago is not evidence about now -- a lifecycle rule, a bucket policy
change or a mistaken cleanup can remove an object, and nothing would have told
us. Anything about to be deleted at the source is re-checked against the
destination first, at the moment of deleting, per object.

Everything here is read-only against both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["AccountCompleteness", "account_completeness", "blocking_reasons"]


@dataclass(slots=True)
class AccountCompleteness:
    """What is known about one CommPeak account's archive state."""

    connection_id: int
    account: str
    brand_id: int
    brand_name: str
    indexed: int
    verified: int
    failed: int
    missing_source: int
    #: Indexed but not verified and not failed -- still in flight.
    in_flight: int
    #: The oldest day the inventory has walked, and the newest. A report that
    #: does not say how much of the estate it covers invites the reader to
    #: assume all of it.
    scanned_from: str | None
    scanned_to: str | None
    #: Recordings with no `cdr_id`. They are archived like any other and are
    #: listed because "we could not match it to a call" is a different thing
    #: from "it is not safely copied", and only the second blocks a delete.
    orphans: int
    notes: list[str] = field(default_factory=list)

    @property
    def unverified(self) -> int:
        """Everything indexed that is not provably in the archive."""
        return max(0, self.indexed - self.verified)

    @property
    def percent_verified(self) -> float:
        if not self.indexed:
            return 0.0
        return round(100.0 * self.verified / self.indexed, 2)

    @property
    def safe_to_delete_from(self) -> bool:
        """True only when `blocking_reasons` finds nothing to say.

        Derived from the reasons rather than computed alongside them, because
        the first version did compute it separately and the two disagreed: two
        accounts reported `safe = yes` while the reasons underneath listed
        "coverage of this account is unknown". A green light whose own
        explanation contradicts it is worse than no light, and on an
        irreversible action it is the failure that matters.

        Deliberately strict: one unverified recording is enough to say no. The
        alternative is a threshold, and a threshold here is a decision in
        advance about how many recordings may be lost.
        """
        return not blocking_reasons(self)


async def account_completeness(session: AsyncSession) -> list[AccountCompleteness]:
    """Per-account counts, for every enabled CommPeak account in scope."""
    rows = (
        await session.execute(
            text(
                """
                SELECT c.id, c.name, c.brand_id, b.name AS brand_name,
                       to_char(c.earliest_day, 'YYYY-MM-DD')          AS scanned_from,
                       to_char(c.inventory_cursor_day, 'YYYY-MM-DD')  AS scanned_to
                FROM commpeak_connections c
                JOIN brands b ON b.id = c.brand_id
                WHERE c.is_enabled
                ORDER BY b.name, c.name
                """
            )
        )
    ).mappings().all()

    out: list[AccountCompleteness] = []
    for row in rows:
        counts = (
            await session.execute(
                text(
                    """
                    SELECT count(*)                                            AS indexed,
                           count(*) FILTER (WHERE verified_at IS NOT NULL)      AS verified,
                           count(*) FILTER (WHERE state = 'FAILED')             AS failed,
                           count(*) FILTER (WHERE state = 'MISSING_SOURCE')     AS missing_source,
                           count(*) FILTER (WHERE cdr_id IS NULL)               AS orphans
                    FROM recordings
                    WHERE connection_id = :c
                    """
                ),
                {"c": row["id"]},
            )
        ).mappings().one()

        item = AccountCompleteness(
            connection_id=row["id"],
            account=row["name"],
            brand_id=row["brand_id"],
            brand_name=row["brand_name"],
            indexed=int(counts["indexed"]),
            verified=int(counts["verified"]),
            failed=int(counts["failed"]),
            missing_source=int(counts["missing_source"]),
            in_flight=max(
                0,
                int(counts["indexed"])
                - int(counts["verified"])
                - int(counts["failed"])
                - int(counts["missing_source"]),
            ),
            scanned_from=row["scanned_from"],
            scanned_to=row["scanned_to"],
            orphans=int(counts["orphans"]),
        )
        if item.scanned_from is None:
            item.notes.append(
                "the inventory has no recorded start day, so how much of this "
                "account has been walked is unknown"
            )
        if item.indexed == 0:
            item.notes.append("nothing indexed for this account")
        out.append(item)
    return out


def blocking_reasons(item: AccountCompleteness) -> list[str]:
    """Why this account may not be deleted from, in words.

    Returned as sentences rather than a boolean because the answer to "why
    can't I turn this on" should not require reading the code.
    """
    reasons: list[str] = []
    if item.indexed == 0:
        reasons.append("nothing has been indexed for this account")
    if item.unverified:
        reasons.append(
            f"{item.unverified:,} of {item.indexed:,} indexed recordings are not "
            "verified in the archive"
        )
    if item.failed:
        reasons.append(f"{item.failed:,} recordings failed to copy")
    if item.scanned_from is None:
        reasons.append(
            "the inventory's coverage of this account is unknown, so the "
            "indexed count cannot be taken as the whole account"
        )
    return reasons


def as_context(items: list[AccountCompleteness]) -> dict[str, Any]:
    """Shape for a template, with the platform-wide totals worked out here."""
    return {
        "accounts": [
            {
                "account": i.account,
                "brand_name": i.brand_name,
                "indexed": i.indexed,
                "verified": i.verified,
                "unverified": i.unverified,
                "failed": i.failed,
                "missing_source": i.missing_source,
                "in_flight": i.in_flight,
                "orphans": i.orphans,
                "percent_verified": i.percent_verified,
                "scanned_from": i.scanned_from,
                "scanned_to": i.scanned_to,
                "safe": i.safe_to_delete_from,
                "blocking": blocking_reasons(i),
                "notes": i.notes,
            }
            for i in items
        ],
        "totals": {
            "indexed": sum(i.indexed for i in items),
            "verified": sum(i.verified for i in items),
            "unverified": sum(i.unverified for i in items),
            "failed": sum(i.failed for i in items),
            "safe_accounts": sum(1 for i in items if i.safe_to_delete_from),
            "accounts": len(items),
        },
        "generated_at": datetime.now(UTC),
    }
