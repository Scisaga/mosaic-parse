"""Strict one-based page range parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable

_ITEM_RE = re.compile(r"^(?P<start>[1-9][0-9]*)(?:\s*-\s*(?P<end>[1-9][0-9]*))?$")


class PageRangeError(ValueError):
    pass


def parse_page_range(
    value: str | None,
    total_pages: int | None = None,
    *,
    max_items: int = 100_000,
) -> list[int]:
    """Expand ``1-5,8`` into sorted unique one-based page numbers.

    ``None`` means all pages only when ``total_pages`` is known; otherwise an
    empty list is returned to represent an unbounded selection.
    """

    if total_pages is not None and total_pages < 1:
        raise PageRangeError("total_pages must be positive")
    if value is None or not value.strip():
        return list(range(1, total_pages + 1)) if total_pages is not None else []

    selected: set[int] = set()
    parts = value.split(",")
    if any(not part.strip() for part in parts):
        raise PageRangeError("page range contains an empty item")
    for raw_item in parts:
        match = _ITEM_RE.fullmatch(raw_item.strip())
        if match is None:
            raise PageRangeError(f"invalid page range item: {raw_item.strip()!r}")
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start > end:
            raise PageRangeError(f"page range start exceeds end: {raw_item.strip()!r}")
        if total_pages is not None and end > total_pages:
            raise PageRangeError(f"page {end} exceeds document page count {total_pages}")
        if end - start + 1 > max_items or len(selected) + end - start + 1 > max_items:
            raise PageRangeError(f"page range selects more than {max_items} pages")
        selected.update(range(start, end + 1))
    return sorted(selected)


def format_page_range(pages: Iterable[int]) -> str:
    values = sorted(set(pages))
    if any(page < 1 for page in values):
        raise PageRangeError("page numbers must be positive")
    if not values:
        return ""
    groups: list[str] = []
    start = previous = values[0]
    for page in values[1:]:
        if page == previous + 1:
            previous = page
            continue
        groups.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    groups.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(groups)


def group_consecutive_pages(pages: Iterable[int]) -> list[tuple[int, int]]:
    """Return minimal inclusive ranges without filling gaps between pages."""

    values = sorted(set(pages))
    if any(page < 1 for page in values):
        raise PageRangeError("page numbers must be positive")
    if not values:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = values[0]
    for page in values[1:]:
        if page == previous + 1:
            previous = page
            continue
        groups.append((start, previous))
        start = previous = page
    groups.append((start, previous))
    return groups
