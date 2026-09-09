"""Correlation tiers, number normalisation and ambiguity handling.

The scenarios here are drawn from the shapes actually seen in the CommPeak
documentation and the sample CDR: an ``@did.commpeak.com`` suffix on ``src``, a
short numeric ``dst``, split recordings sharing a channel id, and busy
extensions with several calls in the same minute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cprec.commpeak.correlate import (
    CdrCandidate,
    MatchMethod,
    correlate,
    correlation_summary,
    normalise_msisdn,
    numbers_agree,
)
from cprec.commpeak.keyparse import parse_key

# The documented example key; its channel id decodes to the same instant as its
# wall-clock field, which is what makes epoch matching viable.
DOC_KEY = "2021/11/11/12/out-441632960770-101-20211111-125343-1636635223.0.flac"
DOC_START = datetime(2021, 11, 11, 12, 53, 43, tzinfo=UTC)


def cdr(**kw) -> CdrCandidate:
    base = {
        "cdr_id": 1,
        "call_uuid": "e9a46b6f-4711-4225-a731-bf338712817d",
        "start_at": DOC_START,
        "src": "441632960770@did.commpeak.com",
        "dst": "101",
        "call_duration": 35,
    }
    return CdrCandidate(**{**base, **kw})


class TestNumberNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("593990899917@did.commpeak.com", "990899917"),
            ("+44 163 296 0770", "632960770"),
            ("00441632960770", "632960770"),
            ("441632960770", "632960770"),
            ("101", "101"),
            ("0007281", "7281"),
            (None, ""),
            ("", ""),
            ("sip:anonymous@host", ""),
        ],
    )
    def test_normalise(self, raw, expected):
        assert normalise_msisdn(raw) == expected

    def test_long_numbers_agree_on_suffix(self):
        assert numbers_agree("441632960770", "+44 1632 960770")
        assert numbers_agree("00441632960770", "441632960770@did.commpeak.com")

    def test_short_extensions_require_exact_match(self):
        """101 must not be treated as agreeing with 9101 -- that would join
        recordings to the wrong agent's calls."""
        assert numbers_agree("101", "101")
        assert not numbers_agree("101", "9101")
        assert not numbers_agree("101", "1010")

    def test_missing_side_never_agrees(self):
        assert not numbers_agree("101", None)
        assert not numbers_agree(None, None)


class TestTiers:
    def test_epoch_exact_when_time_and_number_align(self):
        result = correlate(parse_key(DOC_KEY), [cdr()])
        assert result.method is MatchMethod.EPOCH_EXACT
        assert result.confidence == pytest.approx(0.99)
        assert result.call_uuid == "e9a46b6f-4711-4225-a731-bf338712817d"
        assert result.delta_seconds == 0.0
        assert result.matched

    def test_epoch_only_when_number_disagrees(self):
        """Same instant but a different party: still very likely the same call,
        so it matches -- one tier lower."""
        result = correlate(parse_key(DOC_KEY), [cdr(src="15550001111@did.commpeak.com", dst="999")])
        assert result.method is MatchMethod.EPOCH_ONLY
        assert result.confidence == pytest.approx(0.90)

    def test_epoch_tolerance_boundary(self):
        within = correlate(parse_key(DOC_KEY), [cdr(start_at=DOC_START + timedelta(seconds=2))])
        assert within.method is MatchMethod.EPOCH_EXACT
        outside = correlate(parse_key(DOC_KEY), [cdr(start_at=DOC_START + timedelta(seconds=3))])
        assert outside.method is MatchMethod.TIME_NUMBER, "beyond epoch tolerance, fall back"

    def test_time_number_within_window(self):
        result = correlate(parse_key(DOC_KEY), [cdr(start_at=DOC_START + timedelta(seconds=45))])
        assert result.method is MatchMethod.TIME_NUMBER
        assert result.delta_seconds == pytest.approx(45.0)

    def test_time_only_when_nothing_but_time_agrees(self):
        result = correlate(
            parse_key(DOC_KEY),
            [cdr(start_at=DOC_START + timedelta(seconds=60), src="15550001111", dst="777")],
        )
        assert result.method is MatchMethod.TIME_ONLY
        assert result.method.needs_review

    def test_orphan_when_outside_window(self):
        result = correlate(parse_key(DOC_KEY), [cdr(start_at=DOC_START + timedelta(minutes=10))])
        assert result.method is MatchMethod.ORPHAN
        assert result.cdr_id is None
        assert result.candidates_considered == 1

    def test_orphan_with_no_candidates(self):
        result = correlate(parse_key(DOC_KEY), [])
        assert result.method is MatchMethod.ORPHAN
        assert result.confidence == 0.0

    def test_nearest_candidate_wins_within_a_tier(self):
        near = cdr(cdr_id=7, start_at=DOC_START + timedelta(seconds=10))
        far = cdr(cdr_id=8, start_at=DOC_START + timedelta(seconds=70))
        assert correlate(parse_key(DOC_KEY), [far, near]).cdr_id == 7

    def test_better_tier_beats_nearer_time(self):
        """An exact epoch match must win over a closer-but-weaker candidate."""
        exact = cdr(cdr_id=1, start_at=DOC_START)
        closer_but_weak = cdr(cdr_id=2, start_at=DOC_START + timedelta(seconds=1), src="9", dst="9")
        result = correlate(parse_key(DOC_KEY), [closer_but_weak, exact])
        assert result.cdr_id == 1
        assert result.method is MatchMethod.EPOCH_EXACT


class TestAmbiguity:
    def test_two_equally_good_candidates_are_flagged(self):
        """A busy extension can have two calls a second apart. We record the
        ambiguity and halve confidence rather than pretending to be sure."""
        a = cdr(cdr_id=1, start_at=DOC_START)
        b = cdr(cdr_id=2, start_at=DOC_START)
        result = correlate(parse_key(DOC_KEY), [a, b])
        assert result.ambiguous
        assert result.confidence == pytest.approx(0.495)
        assert result.cdr_id in (1, 2)

    def test_clearly_separated_candidates_are_not_ambiguous(self):
        a = cdr(cdr_id=1, start_at=DOC_START)
        b = cdr(cdr_id=2, start_at=DOC_START + timedelta(seconds=30))
        assert not correlate(parse_key(DOC_KEY), [a, b]).ambiguous


class TestUnparseableKeys:
    def test_salvaged_epoch_still_correlates(self):
        """A key we cannot fully parse still carries an epoch we can join on."""
        parsed = parse_key("2021/11/11/12/recording_1636635223_final.flac")
        assert not parsed.parsed_ok
        result = correlate(parsed, [cdr()])
        assert result.method in (MatchMethod.EPOCH_EXACT, MatchMethod.EPOCH_ONLY)
        assert result.matched, "an unparseable name must not cost us the CDR link"

    def test_key_with_no_time_signal_is_orphan(self):
        parsed = parse_key("misc/notes.flac")
        assert correlate(parsed, [cdr()]).method is MatchMethod.ORPHAN


class TestSplitRecordings:
    def test_parts_share_a_group_key_and_all_match(self):
        """CommPeak emits split recordings as the same channel id with an
        incrementing sequence; every part must resolve to the one call."""
        parts = [
            parse_key(f"2021/11/11/12/out-441632960770-101-20211111-125343-1636635223.{n}.flac")
            for n in range(3)
        ]
        assert {p.seq for p in parts} == {0, 1, 2}
        assert len({p.call_group_key for p in parts}) == 1
        results = [correlate(p, [cdr()]) for p in parts]
        assert all(r.method is MatchMethod.EPOCH_EXACT for r in results)
        assert {r.cdr_id for r in results} == {1}


class TestSummary:
    def test_summary_reports_the_phase0_gate(self):
        parsed = parse_key(DOC_KEY)
        results = [correlate(parsed, [cdr()]) for _ in range(96)]
        results += [correlate(parsed, []) for _ in range(4)]
        summary = correlation_summary(results)
        assert summary["total"] == 100
        assert summary["strong_ratio"] == 0.96
        assert summary["orphan_ratio"] == 0.04
        assert summary["counts"]["epoch_exact"] == 96

    def test_summary_of_empty_input_is_safe(self):
        summary = correlation_summary([])
        assert summary["total"] == 0
        assert summary["strong_ratio"] == 0.0
