"""CDR API client: normalisation, envelope handling and pagination.

The field mapping is pinned against the real payload shape observed from
CommPeak. The transport is not yet confirmed, so the parsing is tested for
tolerance: a renamed envelope key or a missing column must not lose a page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from cprec.commpeak.cdr_client import (
    AuthScheme,
    CdrApiConfig,
    CdrClient,
    normalise_cdr,
    parse_cdr_timestamp,
    poll_window,
)

# The payload shape actually returned by CommPeak.
SAMPLE = {
    "object_type_id": 4,
    "object_id": 0,
    "caller_user_id": None,
    "start_at": "2026-09-08 00:52:17",
    "end_at": "2026-09-08 00:52:51",
    "src": "593990899917@did.commpeak.com",
    "dst": "0007281",
    "dst_country": None,
    "call_uuid": "e9a46b6f-4711-4225-a731-bf338712817d",
    "agent_callerid_name": None,
    "agent_callerid_number": None,
    "client_callerid_name": "593990899917",
    "client_callerid_number": "593990899917",
    "client_hangup_disposition": "agent",
    "call_duration": 35,
    "status": "NORMAL_CLEARING",
    "call_id": 113332,
    "caller_user": "System",
    "public_recording_url": (
        "https://go4rexnew.td.commpeak.com/record/public/113332/"
        "9980774f10c62f717e52f3245befb44bbc2e3834/as/mp3"
    ),
}


class TestTimestamps:
    def test_naive_timestamps_are_read_as_utc(self):
        """CommPeak sends no zone. The recording filenames confirm UTC: the
        documented key's channel id decodes to its wall-clock field exactly."""
        parsed = parse_cdr_timestamp("2026-09-08 00:52:17")
        assert parsed == datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC)

    @pytest.mark.parametrize(
        "value",
        ["2026-09-08T00:52:17", "2026-09-08T00:52:17+00:00", 1788909137, datetime(2026, 1, 1)],
    )
    def test_other_forms_are_accepted(self, value):
        assert parse_cdr_timestamp(value) is not None

    @pytest.mark.parametrize("value", [None, "", "not a date", "08/09/2026"])
    def test_unparseable_values_yield_none(self, value):
        assert parse_cdr_timestamp(value) is None


class TestNormalisation:
    def test_maps_the_real_payload(self):
        row = normalise_cdr(SAMPLE)
        assert row["call_uuid"] == "e9a46b6f-4711-4225-a731-bf338712817d"
        assert row["call_id"] == 113332
        assert row["start_at"] == datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC)
        assert row["call_duration"] == 35
        assert row["status"] == "NORMAL_CLEARING"
        assert row["hangup_disposition"] == "agent"
        assert row["caller_user"] == "System"
        assert row["public_recording_url"].endswith("/as/mp3")

    def test_numbers_are_normalised_for_search_and_correlation(self):
        """Pre-computed on write so number search never pays for it per row."""
        row = normalise_cdr(SAMPLE)
        assert row["src"] == "593990899917@did.commpeak.com"
        assert row["src_norm"] == "990899917"
        assert row["dst_norm"] == "7281"

    def test_direction_is_inferred_from_the_did_suffix(self):
        """CommPeak sends no direction field; an @did. source is an inbound leg."""
        assert normalise_cdr(SAMPLE)["direction"] == "in"
        outbound = {**SAMPLE, "src": "0007281", "dst": "441632960770@carrier", "object_type_id": 1}
        assert normalise_cdr(outbound)["direction"] == "out"

    def test_raw_payload_is_preserved(self):
        """A field we did not model -- or one added later -- must not be lost."""
        row = normalise_cdr({**SAMPLE, "some_new_commpeak_field": "value"})
        assert row["raw"]["some_new_commpeak_field"] == "value"

    def test_missing_fields_do_not_raise(self):
        row = normalise_cdr({"call_uuid": "x", "start_at": "2026-09-08 00:00:00"})
        assert row["src"] is None
        assert row["src_norm"] is None
        assert row["call_duration"] is None

    def test_record_file_is_used_when_public_url_is_absent(self):
        payload = {k: v for k, v in SAMPLE.items() if k != "public_recording_url"}
        payload["record_file"] = "https://example/record/1/x/as/mp3"
        assert normalise_cdr(payload)["public_recording_url"].endswith("/as/mp3")


class TestFetching:
    @respx.mock
    async def test_pages_until_a_short_page(self):
        config = CdrApiConfig(base_url="https://pbx.example", path="/api/cdrs", page_size=2)
        route = respx.get("https://pbx.example/api/cdrs")
        route.side_effect = [
            httpx.Response(200, json={"data": [SAMPLE, SAMPLE]}),
            httpx.Response(200, json={"data": [SAMPLE]}),
        ]
        pages = [
            page
            async for page in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            )
        ]
        assert [len(p) for p in pages] == [2, 1]
        assert route.call_count == 2

    @respx.mock
    @pytest.mark.parametrize("envelope", ["data", "items", "results", "cdrs", "records"])
    async def test_accepts_any_common_envelope(self, envelope):
        """The exact envelope key is not confirmed, so tolerate the usual ones
        rather than dropping a page because of a rename."""
        config = CdrApiConfig(base_url="https://pbx.example", page_size=5)
        respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={envelope: [SAMPLE]})
        )
        pages = [
            page
            async for page in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            )
        ]
        assert pages == [[SAMPLE]]

    @respx.mock
    async def test_accepts_a_bare_list(self):
        config = CdrApiConfig(base_url="https://pbx.example", page_size=5)
        respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json=[SAMPLE])
        )
        pages = [
            page
            async for page in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            )
        ]
        assert pages == [[SAMPLE]]

    @respx.mock
    async def test_empty_response_stops_immediately(self):
        config = CdrApiConfig(base_url="https://pbx.example")
        respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        pages = [
            page
            async for page in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            )
        ]
        assert pages == []

    @respx.mock
    async def test_ignored_pagination_does_not_loop_forever(self):
        """If the API ignores the offset parameter, a naive loop would re-read
        page one indefinitely."""
        config = CdrApiConfig(base_url="https://pbx.example", page_size=1)
        respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"data": [SAMPLE]})
        )
        pages = [
            page
            async for page in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC), max_pages=4
            )
        ]
        assert len(pages) == 4

    @respx.mock
    @pytest.mark.parametrize("code", [401, 403])
    async def test_auth_failures_are_explicit(self, code):
        config = CdrApiConfig(base_url="https://pbx.example")
        respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(code, json={"error": "nope"})
        )
        with pytest.raises(PermissionError):
            async for _ in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            ):
                pass

    @respx.mock
    async def test_bearer_token_is_sent(self):
        config = CdrApiConfig(
            base_url="https://pbx.example", auth=AuthScheme.BEARER, token="tok"
        )
        route = respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
        ):
            pass
        assert route.calls[0].request.headers["authorization"] == "Bearer tok"

    @respx.mock
    async def test_header_auth_scheme(self):
        config = CdrApiConfig(
            base_url="https://pbx.example",
            auth=AuthScheme.HEADER,
            header_name="X-API-Key",
            token="secret-key",
        )
        route = respx.get(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
        ):
            pass
        assert route.calls[0].request.headers["x-api-key"] == "secret-key"


class TestPollWindow:
    def test_first_poll_looks_back_a_day(self):
        now = datetime(2026, 9, 8, 12, tzinfo=UTC)
        start, end = poll_window(None, now=now)
        assert end == now
        assert now - start == timedelta(hours=24)

    def test_subsequent_polls_overlap_the_cursor(self):
        """A CDR is finalised when the call ends, so a long call that started
        inside the previous window may only appear afterwards."""
        now = datetime(2026, 9, 8, 12, 30, tzinfo=UTC)
        cursor = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
        start, end = poll_window(cursor, now=now, overlap_minutes=15)
        assert start == datetime(2026, 9, 8, 11, 45, tzinfo=UTC)
        assert end == now
