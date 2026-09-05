"""
Local filtering and activity-type mapping.

filter_jobs / filter_candidates page inside the connector and filter here.
A wrong operator does not error - it just returns the wrong pipeline, and
whoever reads it believes it.
"""

import pytest

ITEMS = [
    {"id": 1, "title": "Project Manager", "status_id": 10, "date_created": "2026-01-10T00:00:00"},
    {"id": 2, "title": "Business Analyst", "status_id": 20, "date_created": "2026-02-10T00:00:00"},
    {"id": 3, "title": "Solution Architect", "status_id": 10, "date_created": "2026-03-10T00:00:00"},
]


def _ids(rows):
    return [r["id"] for r in rows]


def test_no_filters_returns_everything(api):
    assert _ids(api._apply_local_filters(ITEMS, [])) == [1, 2, 3]


def test_equality_matches(api):
    rows = api._apply_local_filters(ITEMS, [{"field": "status_id", "op": "eq", "value": 10}])
    assert _ids(rows) == [1, 3]


def test_equality_is_the_default_operator(api):
    rows = api._apply_local_filters(ITEMS, [{"field": "status_id", "value": 20}])
    assert _ids(rows) == [2]


def test_equality_compares_as_strings(api):
    """CATS returns ids as ints in some payloads and strings in others."""
    rows = api._apply_local_filters(ITEMS, [{"field": "status_id", "value": "10"}])
    assert _ids(rows) == [1, 3]


def test_contains_is_case_insensitive(api):
    rows = api._apply_local_filters(ITEMS, [{"field": "title", "op": "contains", "value": "ANALYST"}])
    assert _ids(rows) == [2]


def test_contains_with_no_value_matches_nothing(api):
    rows = api._apply_local_filters(ITEMS, [{"field": "title", "op": "contains", "value": None}])
    assert rows == []


def test_date_greater_than_or_equal(api):
    rows = api._apply_local_filters(
        ITEMS, [{"field": "date_created", "op": "gte", "value": "2026-02-01T00:00:00"}]
    )
    assert _ids(rows) == [2, 3]


def test_date_less_than_or_equal(api):
    rows = api._apply_local_filters(
        ITEMS, [{"field": "date_created", "op": "lte", "value": "2026-02-01T00:00:00"}]
    )
    assert _ids(rows) == [1]


def test_filters_combine_with_and(api):
    rows = api._apply_local_filters(ITEMS, [
        {"field": "status_id", "value": 10},
        {"field": "date_created", "op": "gte", "value": "2026-02-01T00:00:00"},
    ])
    assert _ids(rows) == [3]


def test_missing_field_excludes_the_row(api):
    rows = api._apply_local_filters(ITEMS, [{"field": "nonexistent", "value": "x"}])
    assert rows == []


# ---- activity types ------------------------------------------------------


def test_known_type_maps_through(api):
    """Whatever the map holds, a known key must not silently become 'other'."""
    for friendly, expected in api.ACTIVITY_TYPE_MAP.items():
        assert api.map_activity_type(friendly) == expected


def test_type_matching_ignores_case_and_padding(api):
    friendly = next(iter(api.ACTIVITY_TYPE_MAP))
    assert api.map_activity_type(f"  {friendly.upper()}  ") == api.ACTIVITY_TYPE_MAP[friendly]


@pytest.mark.parametrize("value", ["", None, "something CATS has never heard of"])
def test_unknown_and_empty_types_fall_back_to_other(api, value):
    """'other' is the one value CATS always accepts - never fail the write."""
    assert api.map_activity_type(value) == "other"
