"""
auto_shape sits between every tool result and Claude. If it regresses,
nothing errors - responses just quietly arrive in a shape nothing can read,
and every tool looks broken at once.
"""

import pytest


def test_embedded_is_flattened(api):
    shaped = api.auto_shape({"_embedded": {"candidates": [{"id": 1}, {"id": 2}]}})
    assert shaped["candidates"] == [{"id": 1}, {"id": 2}]
    assert "_embedded" not in shaped


def test_links_are_stripped(api):
    shaped = api.auto_shape({"id": 1, "_links": {"self": {"href": "/x"}}})
    assert "_links" not in shaped
    assert shaped["id"] == 1


def test_flattening_never_clobbers_a_real_key(api):
    """A top-level key wins over an _embedded one of the same name."""
    shaped = api.auto_shape(
        {"candidates": ["real"], "_embedded": {"candidates": ["embedded"]}}
    )
    assert shaped["candidates"] == ["real"]


def test_pagination_is_computed(api):
    shaped = api.auto_shape({"total": 250, "count": 100, "page": 2})
    assert shaped["per_page"] == 100
    assert shaped["pages"] == 3
    assert shaped["has_more"] is True


def test_last_page_reports_no_more(api):
    shaped = api.auto_shape({"total": 250, "count": 100, "page": 3})
    assert shaped["pages"] == 3
    assert shaped["has_more"] is False


def test_exact_page_boundary(api):
    """200 records at 100 per page is 2 pages, not 3."""
    shaped = api.auto_shape({"total": 200, "count": 100, "page": 2})
    assert shaped["pages"] == 2
    assert shaped["has_more"] is False


def test_single_partial_page(api):
    shaped = api.auto_shape({"total": 7, "count": 100, "page": 1})
    assert shaped["pages"] == 1
    assert shaped["has_more"] is False


def test_zero_results(api):
    shaped = api.auto_shape({"total": 0, "count": 100, "page": 1})
    assert shaped["pages"] == 0


def test_zero_per_page_does_not_divide_by_zero(api):
    shaped = api.auto_shape({"total": 10, "count": 0, "page": 1})
    assert "pages" not in shaped


@pytest.mark.parametrize("value", [None, [], "text", 42, True])
def test_non_dict_passes_through_untouched(api, value):
    assert api.auto_shape(value) is value


def test_plain_dict_is_left_alone(api):
    original = {"id": 1, "name": "Nicole"}
    assert api.auto_shape(dict(original)) == original
