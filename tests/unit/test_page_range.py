import pytest

from app.utils.page_range import (
    PageRangeError,
    format_page_range,
    group_consecutive_pages,
    parse_page_range,
)


def test_parse_page_range_expands_sorts_and_deduplicates() -> None:
    assert parse_page_range("8, 1-3, 2, 10-11", total_pages=12) == [1, 2, 3, 8, 10, 11]


def test_none_selects_all_known_pages() -> None:
    assert parse_page_range(None, total_pages=3) == [1, 2, 3]
    assert parse_page_range(None) == []


@pytest.mark.parametrize("value", ["0", "3-1", "1,,2", "-2", "1-", "one"])
def test_invalid_ranges_are_rejected(value: str) -> None:
    with pytest.raises(PageRangeError):
        parse_page_range(value)


def test_range_is_bounded_by_document() -> None:
    with pytest.raises(PageRangeError, match="exceeds document page count"):
        parse_page_range("1-4", total_pages=3)


def test_format_page_range_compacts_runs() -> None:
    assert format_page_range([5, 1, 2, 3, 8, 8]) == "1-3,5,8"


def test_group_consecutive_pages_never_fills_sparse_gaps() -> None:
    assert group_consecutive_pages([1, 2, 1000, 1002, 1003]) == [
        (1, 2),
        (1000, 1000),
        (1002, 1003),
    ]
