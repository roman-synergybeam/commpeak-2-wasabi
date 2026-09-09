"""Text messages: the two TextPeak shapes, and the isolation of what they store.

The unit tests pin the mapping, because TextPeak returns two different objects
from two different endpoints and one table has to hold both. The database tests
exist for one hard rule: a new brand-scoped table is not finished until there
is a test proving one organisation cannot read another's rows. These are two
unrelated companies' customer messages.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from c2w.commpeak.sms_client import (
    Direction,
    SmsApiConfig,
    SmsQueryFilters,
    _numeric_suffix,
    normalise_message,
    poll_window,
    store_messages,
)

# Shaped exactly like the reference's own examples for each endpoint.
OUTGOING = {
    "message_uuid": "8f14e45f-ea0d-4f1e-9c37-1a2b3c4d5e6f",
    "sent_at": "2026-03-19 10:15:10",
    "delivered_at": "2026-03-19 10:15:19",
    "status": "delivered",
    "source_number": "Go4Rex",
    "source_name": "Go4Rex",
    "destination_number": "593990899917",
    "country_code": "EC",
    "country_name": "Ecuador",
    "cost": "0.041200",
    "platform": "sms",
    "content": {"body": "Su codigo de verificacion es 448120."},
    "conversation": {"id": 9, "name": "593990899917"},
    "stream": {"name": "ES-Onboarding"},
    "campaign": "welcome-es",
    "external_key": "crm-1042",
}

INCOMING = {
    "message_uuid": "c9f0f895-fb98-4b41-9b0e-1a2b3c4d5e6f",
    "received_at": "2026-03-19 10:20:44",
    "from": "593990899917",
    "to": "Go4Rex",
    "country_code": "EC",
    "country_name": "Ecuador",
    "contact_name": "A. Morales",
    "message_length": 4,
    "body": "STOP",
    "conversation": "593990899917",
    "stream": "ES-Onboarding",
}


class TestNormalisingBothShapes:
    def test_outgoing(self):
        row = normalise_message(OUTGOING, Direction.OUT)
        assert row["direction"] == "out"
        assert row["status"] == "delivered"
        assert row["sent_at"] == datetime(2026, 3, 19, 10, 15, 10, tzinfo=UTC)
        assert row["delivered_at"] == datetime(2026, 3, 19, 10, 15, 19, tzinfo=UTC)
        assert row["received_at"] is None
        # sent_at is the anchor for a message we sent.
        assert row["occurred_at"] == row["sent_at"]
        assert row["destination_number"] == "593990899917"
        # The body is nested under `content` on this endpoint only.
        assert row["body"] == "Su codigo de verificacion es 448120."
        assert row["cost"] == Decimal("0.041200")
        assert row["country_name"] == "Ecuador"

    def test_incoming(self):
        row = normalise_message(INCOMING, Direction.IN)
        assert row["direction"] == "in"
        assert row["received_at"] == datetime(2026, 3, 19, 10, 20, 44, tzinfo=UTC)
        assert row["occurred_at"] == row["received_at"]
        assert row["sent_at"] is None and row["delivered_at"] is None
        # An arrived message has no delivery status and no cost. Recorded as
        # absent rather than invented, so the column means one thing.
        assert row["status"] is None
        assert row["cost"] is None
        assert row["source_number"] == "593990899917"      # `from`
        assert row["destination_number"] == "Go4Rex"       # `to`
        assert row["contact_name"] == "A. Morales"
        assert row["body"] == "STOP"

    def test_nested_objects_are_flattened_to_names(self):
        """These come back as a bare string or an object, depending on the field.

        Both shapes appear in the reference's examples, and rendering
        ``{'id': 9, 'name': '...'}`` into a table cell is not acceptable.
        """
        assert normalise_message(OUTGOING, Direction.OUT)["stream"] == "ES-Onboarding"
        assert normalise_message(OUTGOING, Direction.OUT)["conversation"] == "593990899917"
        assert normalise_message(INCOMING, Direction.IN)["stream"] == "ES-Onboarding"

    def test_the_whole_payload_is_kept(self):
        """So a field not modelled yet can be backfilled without re-fetching."""
        assert normalise_message(OUTGOING, Direction.OUT)["raw"] == OUTGOING

    def test_segments_are_counted_from_length(self):
        short = normalise_message({**OUTGOING, "content": {"body": "a" * 160}}, Direction.OUT)
        assert short["message_length"] == 160
        assert short["segments"] == 1
        long_ = normalise_message({**OUTGOING, "content": {"body": "a" * 161}}, Direction.OUT)
        assert long_["segments"] == 2

    def test_a_missing_cost_is_not_zero(self):
        """A free message and an unknown cost are different facts."""
        assert normalise_message({**OUTGOING, "cost": ""}, Direction.OUT)["cost"] is None
        assert normalise_message({**OUTGOING, "cost": "0"}, Direction.OUT)["cost"] == Decimal(0)


class TestSenderIdIsNotAPhoneNumber:
    """The bug this guards against was live and silent.

    ``normalise_msisdn`` is built for phone numbers and pulls out the trailing
    digits. Given the sender ID "Go4Rex" it returned "4". Written to the
    indexed search column, that made every message from the brand match a
    search for any number ending in 4.
    """

    @pytest.mark.parametrize("sender", ["Go4Rex", "InterMagnum", "VERIFY", "INFO", "SMS"])
    def test_alphanumeric_sender_has_no_numeric_form(self, sender: str):
        assert _numeric_suffix(sender) is None

    @pytest.mark.parametrize(
        "number,expected",
        [
            ("593990899917", "990899917"),
            ("+44 163 296 0770", "632960770"),
            ("00441632960770", "632960770"),
            ("593990899917@did.commpeak.com", "990899917"),
            ("101", "101"),
        ],
    )
    def test_real_numbers_still_normalise(self, number: str, expected: str):
        assert _numeric_suffix(number) == expected

    def test_the_stored_row_reflects_it(self):
        row = normalise_message(OUTGOING, Direction.OUT)
        assert row["source_number"] == "Go4Rex"      # kept, and searchable by text
        assert row["source_norm"] is None            # but not as a number
        assert row["destination_norm"] == "990899917"


class TestRequestShape:
    def test_the_two_endpoints_are_different_paths(self):
        config = SmsApiConfig(token="k")
        assert config.path_for(Direction.OUT) == "/textpeak/streams/messages"
        assert config.path_for(Direction.IN) == "/textpeak/streams/incoming_messages"

    def test_the_key_is_sent_bare(self):
        """No "Bearer" prefix: TextPeak wants the key on its own."""
        assert SmsApiConfig(token="abc123").headers()["Authorization"] == "abc123"

    def test_only_filters_with_a_value_are_sent(self):
        assert SmsQueryFilters().as_params(Direction.OUT) == {}

    def test_status_is_not_sent_to_the_incoming_endpoint(self):
        """It has no such parameter; sending it is at best ignored."""
        filters = SmsQueryFilters(status="delivered", destination="447700900123")
        assert "status" not in filters.as_params(Direction.IN)
        assert filters.as_params(Direction.IN)["destination"] == "447700900123"
        assert filters.as_params(Direction.OUT)["status"] == "delivered"

    def test_dates_use_the_documented_format(self):
        filters = SmsQueryFilters(start=datetime(2026, 3, 19, 10, 15, 10, tzinfo=UTC))
        assert filters.as_params(Direction.OUT)["startDate"] == "2026-03-19 10:15:10"


class TestPollWindow:
    def test_first_run_looks_back_a_week(self):
        now = datetime(2026, 3, 19, 12, 0, tzinfo=UTC)
        start, end = poll_window(None, now=now)
        assert end == now
        assert start == now - timedelta(days=7)

    def test_later_runs_overlap_generously(self):
        """A delivery receipt can arrive hours after the message.

        Without re-reading, a message read one minute after sending keeps the
        status "sent" for ever even once it was delivered.
        """
        now = datetime(2026, 3, 19, 12, 0, tzinfo=UTC)
        cursor = datetime(2026, 3, 19, 11, 0, tzinfo=UTC)
        start, _ = poll_window(cursor, now=now, overlap_hours=24)
        assert start == cursor - timedelta(hours=24)


# ------------------------------------------------------------------ database

TEST_DB = os.environ.get("C2W_TEST_DATABASE_URL")
#: Applied to the database classes individually, not as a module-level
#: ``pytestmark``: that would also skip every unit test above, which needs no
#: database and should run everywhere.
needs_db = pytest.mark.skipif(
    not TEST_DB, reason="set C2W_TEST_DATABASE_URL to a migrated scratch database"
)


@pytest.fixture
async def db():
    engine = create_async_engine(TEST_DB)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _brand(session, slug: str) -> int:
    brand_id = (
        await session.execute(
            text("INSERT INTO brands (name, slug) VALUES (:n, :s) RETURNING id"),
            {"n": slug, "s": slug},
        )
    ).scalar_one()
    await session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS sms_messages_brand_{brand_id} "
            f"PARTITION OF sms_messages FOR VALUES IN ({brand_id})"
        )
    )
    await session.commit()
    return brand_id


async def _scope(session, brand_id: int | None) -> None:
    await session.execute(
        text("SELECT set_config('c2w.brand_id', :b, false)"),
        {"b": str(brand_id) if brand_id else ""},
    )


@needs_db
class TestStoring:
    async def test_insert_then_update_is_counted_correctly(self, db):
        """The insert/update split, which xmax could not provide here.

        ``RETURNING (xmax = 0)`` is the usual way to ask "was this new?", but
        xmax is a system column and asyncpg refuses to read one from an INSERT
        routed through a partitioned parent. This table is partitioned by
        brand, so that form raised for every row.
        """
        slug = f"sms-{uuid.uuid4().hex[:8]}"
        async with db() as s:
            brand_id = await _brand(s, slug)
            await _scope(s, brand_id)

            rows = [
                normalise_message(OUTGOING, Direction.OUT),
                normalise_message(INCOMING, Direction.IN),
            ]
            assert await store_messages(s, brand_id, rows) == (2, 0)
            await s.commit()

            # The same messages again: both updates, no duplicates.
            await _scope(s, brand_id)
            assert await store_messages(s, brand_id, rows) == (0, 2)
            await s.commit()

    async def test_a_late_delivery_receipt_updates_the_status(self, db):
        """The reason the poller re-reads a window instead of only new rows."""
        slug = f"sms-{uuid.uuid4().hex[:8]}"
        async with db() as s:
            brand_id = await _brand(s, slug)
            await _scope(s, brand_id)

            first = normalise_message(
                {**OUTGOING, "status": "sent", "delivered_at": None}, Direction.OUT
            )
            await store_messages(s, brand_id, [first])
            await s.commit()

            await _scope(s, brand_id)
            later = normalise_message(
                {**OUTGOING, "status": "delivered", "delivered_at": "2026-03-19 14:00:00"},
                Direction.OUT,
            )
            assert await store_messages(s, brand_id, [later]) == (0, 1)
            await s.commit()

            await _scope(s, brand_id)
            row = (
                await s.execute(
                    text(
                        "SELECT status, delivered_at FROM sms_messages "
                        "WHERE message_uuid = :u"
                    ),
                    {"u": OUTGOING["message_uuid"]},
                )
            ).one()
            assert row[0] == "delivered"
            assert row[1] == datetime(2026, 3, 19, 14, 0, tzinfo=UTC)

    async def test_a_record_without_an_id_or_time_is_skipped(self, db):
        """Nothing to deduplicate on, or nothing to order by. Skip, not invent."""
        slug = f"sms-{uuid.uuid4().hex[:8]}"
        async with db() as s:
            brand_id = await _brand(s, slug)
            await _scope(s, brand_id)
            unusable = [
                normalise_message({**OUTGOING, "message_uuid": ""}, Direction.OUT),
                normalise_message({**OUTGOING, "sent_at": None, "delivered_at": None},
                                  Direction.OUT),
            ]
            assert await store_messages(s, brand_id, unusable) == (0, 0)
            await s.commit()


@needs_db
class TestBrandIsolation:
    """Two unrelated companies. The new table gets no exception from the rule."""

    async def test_one_brand_cannot_read_the_others_messages(self, db):
        left = f"sms-a-{uuid.uuid4().hex[:6]}"
        right = f"sms-b-{uuid.uuid4().hex[:6]}"
        async with db() as s:
            a = await _brand(s, left)
            b = await _brand(s, right)

            await _scope(s, a)
            await store_messages(s, a, [normalise_message(OUTGOING, Direction.OUT)])
            await s.commit()

            await _scope(s, b)
            await store_messages(
                s, b, [normalise_message({**OUTGOING, "message_uuid": "b-only"}, Direction.OUT)]
            )
            await s.commit()

            # Scoped to B, A's message is not visible at all.
            await _scope(s, b)
            visible = (
                await s.execute(text("SELECT message_uuid FROM sms_messages"))
            ).scalars().all()
            assert visible == ["b-only"]

            # And scoped to A, B's is not.
            await _scope(s, a)
            visible = (
                await s.execute(text("SELECT message_uuid FROM sms_messages"))
            ).scalars().all()
            assert visible == [OUTGOING["message_uuid"]]

    async def test_an_unscoped_session_sees_nothing_rather_than_everything(self, db):
        """Fail closed: forgetting to scope must not expose every brand."""
        slug = f"sms-{uuid.uuid4().hex[:8]}"
        async with db() as s:
            brand_id = await _brand(s, slug)
            await _scope(s, brand_id)
            await store_messages(s, brand_id, [normalise_message(OUTGOING, Direction.OUT)])
            await s.commit()

            await _scope(s, None)
            rows = (await s.execute(text("SELECT count(*) FROM sms_messages"))).scalar_one()
            assert rows == 0

    async def test_writing_into_another_brand_is_refused(self, db):
        """The WITH CHECK half of the policy, which is easy to omit."""
        import sqlalchemy.exc

        left = f"sms-c-{uuid.uuid4().hex[:6]}"
        right = f"sms-d-{uuid.uuid4().hex[:6]}"
        async with db() as s:
            a = await _brand(s, left)
            b = await _brand(s, right)
            await _scope(s, a)
            with pytest.raises(sqlalchemy.exc.DBAPIError):
                await store_messages(s, b, [normalise_message(OUTGOING, Direction.OUT)])
            await s.rollback()
