"""
Date parsing. CATS returns tz-aware timestamps; users type '2026-01-01'.
Mixing the two raises TypeError at comparison time, inside a filter, on a
call the user is waiting on.
"""

from datetime import datetime, timezone

import pytest


@pytest.mark.parametrize("value", [
    "2026-01-15",
    "2026-01-15T09:30:00Z",
    "2026-01-15T09:30:00+00:00",
    "2026-01-15T09:30:00+11:00",
])
def test_accepted_formats_are_always_aware(api, value):
    parsed = api._to_aware(value)
    assert parsed is not None
    assert parsed.tzinfo is not None, "naive datetime will explode on comparison"


def test_naive_input_is_assumed_utc(api):
    assert api._to_aware("2026-01-15T09:30:00").tzinfo == timezone.utc


def test_date_only_parses_to_midnight_utc(api):
    parsed = api._to_aware("2026-01-15")
    assert (parsed.year, parsed.month, parsed.day) == (2026, 1, 15)
    assert parsed.tzinfo == timezone.utc


@pytest.mark.parametrize("value", ["", None, "not a date", "15/01/2026"])
def test_unparseable_returns_none_rather_than_raising(api, value):
    assert api._to_aware(value) is None


def test_offset_is_respected(api):
    """09:30+11:00 is 22:30 the previous day in UTC."""
    parsed = api._to_aware("2026-01-15T09:30:00+11:00")
    assert parsed.astimezone(timezone.utc).hour == 22
    assert parsed.astimezone(timezone.utc).day == 14


# ---- _since_filter -------------------------------------------------------

ITEMS = [
    {"id": 1, "date_modified": "2026-01-10T00:00:00Z"},
    {"id": 2, "date_modified": "2026-01-20T00:00:00Z"},
    {"id": 3, "date_created": "2026-01-25T00:00:00Z"},
    {"id": 4},
]


def test_since_keeps_only_newer_items(api):
    kept = [i["id"] for i in api._since_filter(ITEMS, "2026-01-15")]
    assert kept == [2, 3]


def test_since_is_inclusive_of_the_boundary(api):
    kept = [i["id"] for i in api._since_filter(ITEMS, "2026-01-20T00:00:00Z")]
    assert 2 in kept


def test_since_falls_back_to_date_created(api):
    """An item with no date_modified is judged on when it was created."""
    kept = [i["id"] for i in api._since_filter(ITEMS, "2026-01-22")]
    assert kept == [3]


def test_item_with_no_dates_is_dropped(api):
    kept = [i["id"] for i in api._since_filter(ITEMS, "2026-01-01")]
    assert 4 not in kept


def test_unparseable_since_returns_everything(api):
    """Better to over-return than to silently show an empty pipeline."""
    assert api._since_filter(ITEMS, "rubbish") == ITEMS
