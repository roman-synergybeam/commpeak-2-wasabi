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

from c2w.commpeak.cdr_client import (
    AuthScheme,
    CdrApiConfig,
    CdrClient,
    normalise_cdr,
    parse_cdr_timestamp,
    poll_window,
)

# The documented PBX Stats row, from
# https://docs.commpeak.com/reference/searchcdrs
SAMPLE = {
    "id": "5698",
    "call_start": "2026-09-08 00:52:17",
    "call_end": "2026-09-08 00:52:51",
    "duration": "34",
    "bill_duration": "30",
    "type": "Outbound",
    "destination": "593990899917",
    "caller_id": "0007281",
    "country_name": "Ecuador",
    "agent_name": "Maria Santos",
    "agent_pbxExtension": "102",
    "bridged_agent_name": "",
    "bridged_agent_pbxExtension": "",
    "hangup_cause": "ANSWERED",
    "waiting_time": "3",
    "recording_link": "https://instance.stats.pbx.commpeak.com/rec/5698.mp3",
    "queue_alias": "SALES",
    "queue_name": "Sales ES",
    "desks": "Quito",
    "custom_fields": "{}",
    "cost": "0.0123",
    "uniqueid": "1757292737.3",
}

#: The other CDR source's shape, which some instances return instead.
LEGACY_SAMPLE = {
    "start_at": "2026-09-08 00:52:17",
    "end_at": "2026-09-08 00:52:51",
    "src": "593990899917@did.commpeak.com",
    "dst": "0007281",
    "call_uuid": "e9a46b6f-4711-4225-a731-bf338712817d",
    "call_duration": 35,
    "status": "NORMAL_CLEARING",
    "call_id": 113332,
    "caller_user": "System",
    "client_hangup_disposition": "agent",
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
    def test_maps_the_documented_payload(self):
        row = normalise_cdr(SAMPLE)
        assert row["call_uuid"] == "1757292737.3"
        assert row["call_id"] == 5698
        assert row["start_at"] == datetime(2026, 9, 8, 0, 52, 17, tzinfo=UTC)
        assert row["call_duration"] == 34
        assert row["status"] == "ANSWERED"
        assert row["public_recording_url"].endswith(".mp3")

    def test_the_destination_country_is_carried_through(self):
        """It is a column on the calls page, and the API supplies it -- this
        system used to have nowhere to get it from."""
        assert normalise_cdr(SAMPLE)["dst_country"] == "Ecuador"

    def test_the_agent_who_handled_the_call_is_carried_through(self):
        row = normalise_cdr(SAMPLE)
        assert row["agent_name"] == "Maria Santos"
        assert row["agent_extension"] == "102"

    def test_numbers_are_normalised_for_search_and_correlation(self):
        """Pre-computed on write so number search never pays for it per row."""
        row = normalise_cdr(SAMPLE)
        assert row["src_norm"] == "7281"
        assert row["dst_norm"] == "990899917"

    def test_direction_comes_from_the_type_field(self):
        assert normalise_cdr(SAMPLE)["direction"] == "out"
        assert normalise_cdr({**SAMPLE, "type": "Inbound"})["direction"] == "in"
        assert normalise_cdr({**SAMPLE, "type": ""})["direction"] is None

    def test_durations_survive_being_strings_or_clock_times(self):
        """They arrive as strings, and sometimes as H:MM:SS."""
        assert normalise_cdr({**SAMPLE, "duration": "905"})["call_duration"] == 905
        assert normalise_cdr({**SAMPLE, "duration": "15:05"})["call_duration"] == 905
        assert normalise_cdr({**SAMPLE, "duration": "1:00:30"})["call_duration"] == 3630
        assert normalise_cdr({**SAMPLE, "duration": ""})["call_duration"] is None

    def test_the_other_cdr_shape_still_maps(self):
        """Two sources exist and their field names do not agree; an instance
        may be on either."""
        row = normalise_cdr(LEGACY_SAMPLE)
        assert row["call_uuid"] == "e9a46b6f-4711-4225-a731-bf338712817d"
        assert row["call_duration"] == 35
        assert row["status"] == "NORMAL_CLEARING"
        assert row["direction"] == "in", "an @did. source is an inbound DID leg"
        assert row["src_norm"] == "990899917"

    def test_raw_payload_is_preserved(self):
        """A field we did not model -- or one added later -- must not be lost."""
        row = normalise_cdr({**SAMPLE, "some_new_commpeak_field": "value"})
        assert row["raw"]["some_new_commpeak_field"] == "value"

    def test_missing_fields_do_not_raise(self):
        row = normalise_cdr({"call_uuid": "x", "start_at": "2026-09-08 00:00:00"})
        assert row["src"] is None
        assert row["src_norm"] is None
        assert row["call_duration"] is None

    def test_a_recording_link_is_found_under_any_of_its_names(self):
        """Three names for the same thing across the two sources."""
        base = {k: v for k, v in SAMPLE.items() if k != "recording_link"}
        for name in ("recording_link", "public_recording_url", "record_file"):
            row = normalise_cdr({**base, name: "https://example/rec/1/as/mp3"})
            assert row["public_recording_url"] == "https://example/rec/1/as/mp3"
        assert normalise_cdr(base)["public_recording_url"] is None


class TestFetching:
    @respx.mock
    async def test_pages_until_a_short_page(self):
        config = CdrApiConfig(base_url="https://pbx.example", path="/api/cdrs", page_size=2)
        route = respx.post("https://pbx.example/api/cdrs")
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
    @pytest.mark.parametrize("envelope", ["cdrs", "data", "items", "results", "records"])
    async def test_accepts_any_common_envelope(self, envelope):
        """The exact envelope key is not confirmed, so tolerate the usual ones
        rather than dropping a page because of a rename."""
        config = CdrApiConfig(base_url="https://pbx.example", page_size=5)
        respx.post(url__startswith="https://pbx.example").mock(
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
        respx.post(url__startswith="https://pbx.example").mock(
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
        respx.post(url__startswith="https://pbx.example").mock(
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
        respx.post(url__startswith="https://pbx.example").mock(
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
        respx.post(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(code, json={"error": "nope"})
        )
        with pytest.raises(PermissionError):
            async for _ in CdrClient(config).fetch_range(
                datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
            ):
                pass

    @respx.mock
    async def test_the_request_is_a_form_encoded_post(self):
        """It is a POST with a form body, not a GET with query parameters."""
        config = CdrApiConfig(base_url="https://pbx.example", page_size=5)
        route = respx.post(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"cdrs": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
        ):
            pass
        request = route.calls[0].request
        assert request.method == "POST"
        assert request.headers["content-type"].startswith(
            "application/x-www-form-urlencoded"
        )
        body = request.content.decode()
        assert "page=1" in body
        assert "cdrs_per_page=5" in body
        assert "from=2026-09-08" in body
        assert "till=2026-09-09" in body

    @respx.mock
    async def test_filters_are_sent_when_given(self):
        from c2w.commpeak.cdr_client import CdrQueryFilters

        config = CdrApiConfig(base_url="https://pbx.example", page_size=5)
        route = respx.post(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"cdrs": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC),
            datetime(2026, 9, 9, tzinfo=UTC),
            filters=CdrQueryFilters(country="EC,BR", direction="outbound", successful=True),
        ):
            pass
        body = route.calls[0].request.content.decode()
        assert "country=EC%2CBR" in body
        assert "direction=outbound" in body
        assert "successful=1" in body

    @respx.mock
    async def test_bearer_token_is_sent(self):
        config = CdrApiConfig(
            base_url="https://pbx.example", auth=AuthScheme.BEARER, token="tok"
        )
        route = respx.post(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
        ):
            pass
        assert route.calls[0].request.headers["authorization"] == "Bearer tok"

    @respx.mock
    async def test_the_api_key_goes_in_x_api_key_by_default(self):
        """`X-API-KEY`, established against the live endpoint.

        This test asserted `Authorization` because the module docstring said
        so. Both headers were then sent to a real PBX Stats instance with a
        deliberately invalid key:

            X-API-KEY:     {"error":"No user found for given API key."}
            Authorization: {"error":"No valid API key was given."}

        The second is word-for-word what the endpoint returns when no header
        is sent at all, so `Authorization` is ignored, while `X-API-KEY` was
        read and looked up. Sending the wrong one would have produced a 401
        with a perfectly good key -- and the obvious conclusion from a 401 is
        that the key is wrong, so this would have cost somebody a re-issued
        key and an afternoon.
        """
        config = CdrApiConfig(base_url="https://pbx.example", token="an-api-key")
        route = respx.post(url__startswith="https://pbx.example").mock(
            return_value=httpx.Response(200, json={"cdrs": []})
        )
        async for _ in CdrClient(config).fetch_range(
            datetime(2026, 9, 8, tzinfo=UTC), datetime(2026, 9, 9, tzinfo=UTC)
        ):
            pass
        sent = route.calls[0].request.headers
        assert sent["x-api-key"] == "an-api-key"
        # And not the one the endpoint ignores, which is the actual regression.
        assert "authorization" not in sent

    @respx.mock
    async def test_header_auth_scheme(self):
        config = CdrApiConfig(
            base_url="https://pbx.example",
            auth=AuthScheme.HEADER,
            header_name="X-API-Key",
            token="secret-key",
        )
        route = respx.post(url__startswith="https://pbx.example").mock(
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
