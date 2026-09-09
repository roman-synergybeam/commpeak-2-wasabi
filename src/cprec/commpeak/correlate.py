"""Correlate recording objects with CDR rows.

This is the riskiest logic in the platform and the reason Phase 0 exists.  The
recording key gives us direction, a phone number, an extension, a wall-clock
timestamp and a FreeSWITCH channel id::

    out-441632960770-101-20211111-125343-1636635223.0.flac

The CDR gives us ``call_uuid``, ``call_id``, ``start_at``/``end_at``, ``src``
(``593990899917@did.commpeak.com``), ``dst`` (``0007281``) and
``call_duration``.  There is no shared identifier, so the join must be inferred
and its reliability must be measurable.

Two properties of the data make that tractable:

1. The channel id is a unix epoch second taken at channel creation.  In the
   documented example ``1636635223`` decodes to ``2021-11-11 12:53:43Z``, which
   is exactly the filename's wall-clock field -- so the id is call-start in UTC
   and lands within seconds of the CDR's ``start_at``.
2. Phone numbers appear in both, though with differing prefixes and an
   ``@domain`` suffix on the CDR side, so they are compared as digit suffixes.

Every recording gets a tier and a numeric confidence, and unmatched recordings
become ``orphan`` rather than being dropped.  A recording that fails to
correlate is still inventoried, still offloaded and still playable -- losing
audio because a join failed would be far worse than showing a call with thin
metadata.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from cprec.commpeak.keyparse import ParsedKey

__all__ = [
    "EPOCH_TOLERANCE",
    "TIME_WINDOW",
    "CdrCandidate",
    "MatchMethod",
    "MatchResult",
    "correlate",
    "correlation_summary",
    "normalise_msisdn",
    "numbers_agree",
]

#: How far the channel-id epoch may sit from the CDR ``start_at`` and still be
#: treated as the same call.  Channel creation slightly precedes the CDR's
#: recorded start, so this is deliberately asymmetric-tolerant but tight.
EPOCH_TOLERANCE: Final[timedelta] = timedelta(seconds=2)

#: Fallback window when the epoch is unavailable or disagrees.  Wide enough to
#: absorb PBX clock skew, narrow enough that a busy extension rarely has two
#: distinct calls inside it.
TIME_WINDOW: Final[timedelta] = timedelta(seconds=90)

#: Compare at most this many trailing digits, so that 00441632960770,
#: +441632960770 and 441632960770 all agree.
_SUFFIX_DIGITS: Final[int] = 9

_NON_DIGITS: Final[re.Pattern[str]] = re.compile(r"\D+")


class MatchMethod(enum.StrEnum):
    """How a recording was tied to a CDR, best first."""

    EPOCH_EXACT = "epoch_exact"
    EPOCH_ONLY = "epoch_only"
    TIME_NUMBER = "time_number"
    TIME_ONLY = "time_only"
    ORPHAN = "orphan"

    @property
    def confidence(self) -> float:
        return _CONFIDENCE[self]

    @property
    def needs_review(self) -> bool:
        """Tiers weak enough that an operator should sample-check them."""
        return self in (MatchMethod.TIME_ONLY, MatchMethod.ORPHAN)


_CONFIDENCE: Final[dict[MatchMethod, float]] = {
    MatchMethod.EPOCH_EXACT: 0.99,
    MatchMethod.EPOCH_ONLY: 0.90,
    MatchMethod.TIME_NUMBER: 0.75,
    MatchMethod.TIME_ONLY: 0.40,
    MatchMethod.ORPHAN: 0.0,
}

_RANK: Final[dict[MatchMethod, int]] = {
    MatchMethod.EPOCH_EXACT: 0,
    MatchMethod.EPOCH_ONLY: 1,
    MatchMethod.TIME_NUMBER: 2,
    MatchMethod.TIME_ONLY: 3,
    MatchMethod.ORPHAN: 4,
}


@dataclass(frozen=True, slots=True)
class CdrCandidate:
    """The subset of a CDR row correlation needs."""

    cdr_id: int
    call_uuid: str
    start_at: datetime
    src: str | None = None
    dst: str | None = None
    call_duration: int | None = None
    agent_extension: str | None = None

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(seconds=self.call_duration or 0)


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Outcome of correlating one recording."""

    method: MatchMethod
    cdr_id: int | None = None
    call_uuid: str | None = None
    confidence: float = 0.0
    #: Seconds between the recording's inferred start and the CDR's ``start_at``.
    delta_seconds: float | None = None
    #: True when a second candidate matched at the same tier and a comparable
    #: time distance.  Ambiguous matches are recorded, not silently resolved.
    ambiguous: bool = False
    candidates_considered: int = 0

    @property
    def matched(self) -> bool:
        return self.cdr_id is not None


def normalise_msisdn(value: str | None) -> str:
    """Reduce a CDR or filename number to comparable trailing digits.

    Handles ``593990899917@did.commpeak.com``, ``+44 163 296 0770``,
    ``00441632960770`` and bare extensions alike.
    """
    if not value:
        return ""
    head = value.split("@", 1)[0]
    digits = _NON_DIGITS.sub("", head)
    if not digits:
        return ""
    trimmed = digits.lstrip("0") or digits
    return trimmed[-_SUFFIX_DIGITS:] if len(trimmed) > _SUFFIX_DIGITS else trimmed


def numbers_agree(a: str | None, b: str | None) -> bool:
    """True when two numbers agree on their comparable suffix.

    Short values (extensions such as ``101``) must match exactly rather than by
    suffix, otherwise ``101`` would spuriously agree with ``...9101``.
    """
    na, nb = normalise_msisdn(a), normalise_msisdn(b)
    if not na or not nb:
        return False
    if len(na) < 6 or len(nb) < 6:
        return na == nb
    shortest = min(len(na), len(nb))
    return na[-shortest:] == nb[-shortest:]


def _recording_number_matches(parsed: ParsedKey, cdr: CdrCandidate) -> bool:
    """Does the filename's number appear on either leg of this CDR?"""
    if numbers_agree(parsed.number, cdr.src) or numbers_agree(parsed.number, cdr.dst):
        return True
    # The filename's extension field also shows up as dst or agent extension on
    # inbound legs, so treat that as number agreement too.
    return bool(parsed.extension) and (
        numbers_agree(parsed.extension, cdr.dst)
        or numbers_agree(parsed.extension, cdr.agent_extension)
    )


def _reference_times(parsed: ParsedKey) -> list[datetime]:
    """Candidate start instants for the recording, best first."""
    out: list[datetime] = []
    if (uid := parsed.uniqueid_time) is not None:
        out.append(uid)
    if parsed.started_at is not None and parsed.started_at not in out:
        out.append(parsed.started_at)
    if parsed.prefix_hour is not None and not out:
        out.append(parsed.prefix_hour)
    return out


def _classify(parsed: ParsedKey, cdr: CdrCandidate) -> tuple[MatchMethod, float] | None:
    """Best tier for one (recording, CDR) pair, or None if they cannot match."""
    number_ok = _recording_number_matches(parsed, cdr)
    start = cdr.start_at.astimezone(UTC)

    epoch_time = parsed.uniqueid_time
    if epoch_time is not None:
        delta = abs((epoch_time - start).total_seconds())
        if delta <= EPOCH_TOLERANCE.total_seconds():
            return (
                MatchMethod.EPOCH_EXACT if number_ok else MatchMethod.EPOCH_ONLY,
                delta,
            )

    for ref in _reference_times(parsed):
        delta = abs((ref.astimezone(UTC) - start).total_seconds())
        if delta <= TIME_WINDOW.total_seconds():
            return (MatchMethod.TIME_NUMBER if number_ok else MatchMethod.TIME_ONLY, delta)
    return None


def correlate(parsed: ParsedKey, candidates: list[CdrCandidate]) -> MatchResult:
    """Pick the best CDR for one recording.

    ``candidates`` should already be narrowed by tenant and a coarse time range
    -- the caller does that with an indexed query; this function only ranks.
    """
    scored: list[tuple[int, float, MatchMethod, CdrCandidate]] = []
    for cdr in candidates:
        verdict = _classify(parsed, cdr)
        if verdict is None:
            continue
        method, delta = verdict
        scored.append((_RANK[method], delta, method, cdr))

    if not scored:
        return MatchResult(
            method=MatchMethod.ORPHAN,
            confidence=0.0,
            candidates_considered=len(candidates),
        )

    scored.sort(key=lambda row: (row[0], row[1]))
    rank, delta, method, best = scored[0]

    ambiguous = any(
        other_rank == rank and abs(other_delta - delta) <= 1.0 and other.cdr_id != best.cdr_id
        for other_rank, other_delta, _, other in scored[1:]
    )
    confidence = method.confidence * (0.5 if ambiguous else 1.0)

    return MatchResult(
        method=method,
        cdr_id=best.cdr_id,
        call_uuid=best.call_uuid,
        confidence=round(confidence, 4),
        delta_seconds=round(delta, 3),
        ambiguous=ambiguous,
        candidates_considered=len(candidates),
    )


def correlation_summary(results: list[MatchResult]) -> dict[str, object]:
    """Aggregate match tiers for the Phase 0 accuracy gate.

    ``strong_ratio`` is the number the gate is written against: at least 95% of
    a real sample should land in ``epoch_exact``/``epoch_only``/``time_number``
    before the correlator is trusted on a full bucket.
    """
    counts: dict[str, int] = {m.value: 0 for m in MatchMethod}
    ambiguous = 0
    for r in results:
        counts[r.method.value] += 1
        ambiguous += int(r.ambiguous)
    total = len(results) or 1
    strong = (
        counts[MatchMethod.EPOCH_EXACT.value]
        + counts[MatchMethod.EPOCH_ONLY.value]
        + counts[MatchMethod.TIME_NUMBER.value]
    )
    return {
        "total": len(results),
        "counts": counts,
        "ambiguous": ambiguous,
        "strong_ratio": round(strong / total, 4),
        "orphan_ratio": round(counts[MatchMethod.ORPHAN.value] / total, 4),
    }
