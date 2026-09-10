"""Correlation tiers, number normalisation and ambiguity handling.

The scenarios here are drawn from the shapes actually seen in the CommPeak
documentation and the sample CDR: an ``@did.commpeak.com`` suffix on ``src``, a
short numeric ``dst``, split recordings sharing a channel id, and busy
extensions with several calls in the same minute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from c2w.commpeak.correlate import (
    CdrCandidate,
    MatchMethod,
    correlate,
    correlation_summary,
    normalise_msisdn,
    numbers_agree,
)
from c2w.commpeak.keyparse import parse_key

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


class TestTheTwoLayoutsThatAreActuallyLive:
    """Measured against the eight production buckets, not the documentation.

    Two things the published key layout gets wrong for this estate, and both
    were silently costing us data:

    * Keys sit under a `recordings/` root and have **no hour folder**. The
      scanner listed `YYYY/MM/DD/HH/`, matched nothing, and completed
      successfully -- 170 incremental runs, all `ok = true`, all
      `discovered = 0`.
    * The basename comes in two orders. Go4Rex's PBX writes the documented one
      (direction first, channel id last); InterMagnum's writes the channel id
      **first**. Both are live right now.
    """

    GO4REX = (
        "recordings/2022/12/26/"
        "in-99150321131757-5031470050247407092-20221226-152523-1672068323.91098.flac"
    )
    INTERMAGNUM = (
        "recordings/2026/09/08/1788871734.100994-out-005551999752466-201-20260908-124856.flac"
    )
    INTERNAL = "recordings/2026/09/08/1788871734.100995-internal-201-201-20260908-124854.flac"
    DOCUMENTED = "/2025/11/11/02/out-441632960770-101-20211111-125343-1636635223.0.flac"

    def test_the_channel_id_first_layout_is_parsed(self):
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.INTERMAGNUM)
        assert parsed.direction == "out"
        # The regression this guards: the leading channel id is not the number.
        assert parsed.number == "005551999752466"
        assert int(parsed.uniqueid) == 1788871734
        assert parsed.extension == "201"
        assert parsed.is_audio

    def test_the_channel_id_is_never_mistaken_for_the_number(self):
        """The failure mode, stated as its own test.

        Before the channel-id-first pattern existed the salvage path recovered
        the uniqueid and the timestamp but returned the channel id as `number`,
        because it is the first long digit run in the basename. That is worse
        than returning nothing: `numbers_agree` uses it during correlation and
        it lands in the indexed search column, so it manufactures confident
        false matches instead of an obvious gap.
        """
        from c2w.commpeak.keyparse import parse_key

        for key in (self.INTERMAGNUM, self.INTERNAL):
            parsed = parse_key(key)
            # Compared as strings on purpose: `number` is a str and `uniqueid`
            # an int, so a bare `!=` is true whatever they hold and the
            # assertion would pass even with the bug present.
            assert str(parsed.number) != str(parsed.uniqueid), key

    def test_the_documented_layout_still_parses(self):
        """Adding a pattern must not cost the one the docs describe."""
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.DOCUMENTED)
        assert parsed.direction == "out"
        assert parsed.number == "441632960770"
        assert parsed.extension == "101"
        assert int(parsed.uniqueid) == 1636635223

    def test_both_live_layouts_recover_a_usable_anchor(self):
        """The epoch and the wall clock must agree, or correlation degrades."""
        from c2w.commpeak.keyparse import parse_key

        for key in (self.GO4REX, self.INTERMAGNUM, self.INTERNAL, self.DOCUMENTED):
            parsed = parse_key(key)
            assert parsed.uniqueid_time is not None, key
            assert parsed.started_at is not None, key
            assert abs((parsed.uniqueid_time - parsed.started_at).total_seconds()) <= 2, key

    def test_the_day_prefix_has_a_root_and_no_hour(self):
        from datetime import UTC, datetime

        from c2w.commpeak.keyparse import DEFAULT_KEY_ROOT, day_prefix

        moment = datetime(2026, 9, 8, 13, 4, tzinfo=UTC)
        assert day_prefix(moment) == "recordings/2026/09/08/"
        assert DEFAULT_KEY_ROOT == "recordings/"
        # An empty root is legitimate -- an account whose dates sit at the top.
        assert day_prefix(moment, "") == "2026/09/08/"

    def test_day_prefixes_are_whole_days_oldest_first(self):
        from datetime import UTC, datetime

        from c2w.commpeak.keyparse import iter_day_prefixes

        got = list(
            iter_day_prefixes(
                datetime(2026, 8, 30, 23, 59, tzinfo=UTC),
                datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
            )
        )
        assert got == [
            "recordings/2026/08/30/",
            "recordings/2026/08/31/",
            "recordings/2026/09/01/",
        ]

    def test_a_key_under_the_non_date_branch_yields_no_prefix_time(self):
        """`go4rex.pbx` really has `recordings/default/997/tmp/`."""
        from c2w.commpeak.keyparse import parse_key

        assert parse_key("recordings/default/997/tmp/x.flac").prefix_hour is None

    def test_the_prefix_time_falls_back_to_midnight_on_a_day_path(self):
        from datetime import UTC, datetime

        from c2w.commpeak.keyparse import parse_key

        assert parse_key(self.GO4REX).prefix_hour == datetime(2022, 12, 26, tzinfo=UTC)
        # And the documented four-component form keeps its hour.
        assert parse_key(self.DOCUMENTED).prefix_hour == datetime(2025, 11, 11, 2, tzinfo=UTC)


class TestTheDialerNamesRecordingsWithAUuid:
    """`3de0cd70-91be-4b95-8242-1028016138b4.flac` — and nothing else.

    A UUID is 32 hex characters, and hex contains digits. The salvage path
    found `1028016138` inside `8242-1028016138b4`, read it as a FreeSWITCH
    epoch, and produced a start time of 2002-07-30; the same digits then became
    the phone number. Across the four Dialer accounts -- 100,000 recordings,
    two thirds of the archive -- every row carried a confidently wrong
    timestamp scattered between 2001 and 2032, a wrong number, and no
    direction.

    Wrong metadata is worse than none: it correlates against real CDRs and puts
    recordings decades away from the call that made them.
    """

    KEY = "recordings/2026/09/08/3de0cd70-91be-4b95-8242-1028016138b4.flac"

    def test_no_epoch_is_invented_out_of_hex(self):
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.KEY)
        assert parsed.uniqueid is None, parsed.uniqueid
        assert parsed.number is None, parsed.number

    def test_the_start_time_comes_from_the_folder(self):
        """Real, and the only date in the key."""
        from datetime import UTC, datetime

        from c2w.commpeak.keyparse import parse_key

        assert parse_key(self.KEY).started_at == datetime(2026, 9, 8, tzinfo=UTC)

    def test_the_uuid_is_kept(self):
        """It is the Dialer CDR's `call_uuid`, so it is the join key."""
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.KEY)
        assert parsed.call_uuid == "3de0cd70-91be-4b95-8242-1028016138b4"
        assert parsed.parsed_ok, "a known shape, not a salvage"
        assert parsed.is_audio

    def test_a_matching_uuid_outranks_every_time_window(self):
        from datetime import UTC, datetime

        from c2w.commpeak.correlate import CdrCandidate, MatchMethod, correlate
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.KEY)
        # A CDR with the same uuid but a start time hours from the folder date,
        # and a decoy that is temporally closer. The uuid must still win.
        same_uuid = CdrCandidate(
            cdr_id=1,
            call_uuid="3DE0CD70-91BE-4B95-8242-1028016138B4",
            start_at=datetime(2026, 9, 8, 17, 30, tzinfo=UTC),
        )
        decoy = CdrCandidate(
            cdr_id=2,
            call_uuid="99999999-9999-9999-9999-999999999999",
            start_at=datetime(2026, 9, 8, 0, 0, 30, tzinfo=UTC),
        )
        result = correlate(parsed, [decoy, same_uuid])
        assert result.method == MatchMethod.UUID_EXACT, result.method
        assert result.cdr_id == 1
        assert result.confidence == 1.0

    def test_the_other_two_layouts_are_untouched(self):
        """Adding a pattern must not cost the ones that already worked."""
        from c2w.commpeak.keyparse import parse_key

        pbx = parse_key(
            "recordings/2022/12/26/"
            "in-99150321131757-503-20221226-152523-1672068323.9.flac"
        )
        assert pbx.number == "99150321131757"
        assert pbx.call_uuid is None

        first = parse_key(
            "recordings/2026/09/08/12/"
            "1788871734.100994-out-005551999752466-201-20260908-124856.flac"
        )
        assert first.number == "005551999752466"
        assert first.call_uuid is None

    TRANSFER = (
        "recordings/2026/09/04/"
        "7b8d594d-11b4-450a-bd7f-1409311445bc_transfer4_1788800177.flac"
    )

    def test_a_transferred_leg_uses_its_trailing_epoch(self):
        """The fourth layout, and the same trap a second time.

        `<uuid>_transfer4_<epoch>.flac`. The salvage read `1409311445` out of
        `bd7f-1409311445bc` -- inside the uuid -- and dated the recording to
        2014, while the real channel epoch sat at the end of the name. 2,557
        rows carried that.
        """
        from datetime import UTC, datetime

        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key(self.TRANSFER)
        assert parsed.call_uuid == "7b8d594d-11b4-450a-bd7f-1409311445bc"
        assert parsed.uniqueid == 1788800177
        assert parsed.started_at == datetime(2026, 9, 7, 16, 56, 17, tzinfo=UTC)
        # Not the decoy inside the uuid.
        assert parsed.uniqueid != 1409311445
        assert parsed.started_at.year == 2026

    def test_the_transfer_leg_is_kept(self):
        """Legs of one call must be distinguishable, or they look duplicate."""
        from c2w.commpeak.keyparse import parse_key

        assert parse_key(self.TRANSFER).extension == "transfer4"

    def test_no_layout_dates_a_recording_outside_the_plausible_range(self):
        """A guard on the class of bug rather than on its four instances.

        Every one of these was a plausible ten-digit run found somewhere it
        did not belong. The cheapest way to catch the next one is to assert
        that no known layout produces a date the business cannot have.
        """
        from c2w.commpeak.keyparse import parse_key

        for key in (self.KEY, self.TRANSFER):
            parsed = parse_key(key)
            assert parsed.started_at is not None, key
            assert 2018 <= parsed.started_at.year <= 2035, (key, parsed.started_at)

    def test_an_uppercase_uuid_is_still_recognised(self):
        from c2w.commpeak.keyparse import parse_key

        parsed = parse_key("recordings/2026/09/08/3DE0CD70-91BE-4B95-8242-1028016138B4.FLAC")
        assert parsed.call_uuid == "3de0cd70-91be-4b95-8242-1028016138b4"
        assert parsed.is_audio
