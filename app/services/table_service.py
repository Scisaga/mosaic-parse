"""Docling table export and conservative cross-page logical-table assembly."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.models.parse_result import PageDiagnostics, PageParseResult, PageSourceKind


@dataclass(slots=True)
class TableCellFragment:
    row: int
    column: int
    row_span: int
    column_span: int
    text: str
    normalized_bbox: tuple[float, float, float, float] | None = None
    is_column_header: bool = False
    is_row_header: bool = False


@dataclass(slots=True)
class TableFragment:
    fragment_id: str
    page_number: int
    ordinal: int
    normalized_bbox: tuple[float, float, float, float]
    num_rows: int
    num_cols: int
    rows: list[list[str]]
    column_boundaries: list[float]
    markdown: str
    rendered: str
    valid: bool = True
    invalid_reasons: list[str] = field(default_factory=list)
    logical_table_id: str | None = None
    has_column_header: bool = False
    caption: str | None = None
    unit_text: str | None = None
    source_kind: str = "docling"
    cells: list[TableCellFragment] = field(default_factory=list)
    cell_evidence: list[list[dict[str, str | None]]] = field(default_factory=list)

    @property
    def header_signature(self) -> str:
        return "|".join(
            re.sub(r"\s+", "", cell).casefold() for cell in (self.rows[0] if self.rows else [])
        )


def _escape_cell(value: str) -> str:
    return html.escape(value.replace("\n", " ").strip(), quote=False).replace("|", "&#124;")


def render_gfm_rows(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    normalized = [row + [""] * (columns - len(row)) for row in rows]
    header = normalized[0]
    body = normalized[1:]
    lines = ["| " + " | ".join(_escape_cell(cell) for cell in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(columns)) + " |")
    lines.extend("| " + " | ".join(_escape_cell(cell) for cell in row) + " |" for row in body)
    return "\n".join(lines)


def _anchored_rows(table_data: object) -> tuple[list[list[str]], list[str]]:
    rows = int(getattr(table_data, "num_rows", 0) or 0)
    columns = int(getattr(table_data, "num_cols", 0) or 0)
    reasons: list[str] = []
    if rows <= 0 or columns <= 0 or rows > 2_000 or columns > 100:
        return [], ["invalid_dimensions"]
    output = [["" for _ in range(columns)] for _ in range(rows)]
    occupied: set[tuple[int, int]] = set()
    for cell in getattr(table_data, "table_cells", []) or []:
        start_row = int(getattr(cell, "start_row_offset_idx", -1))
        end_row = int(getattr(cell, "end_row_offset_idx", -1))
        start_col = int(getattr(cell, "start_col_offset_idx", -1))
        end_col = int(getattr(cell, "end_col_offset_idx", -1))
        if not (0 <= start_row < end_row <= rows and 0 <= start_col < end_col <= columns):
            reasons.append("cell_out_of_bounds")
            continue
        positions = {
            (row, column)
            for row in range(start_row, end_row)
            for column in range(start_col, end_col)
        }
        if occupied & positions:
            reasons.append("overlapping_cells")
            continue
        occupied.update(positions)
        output[start_row][start_col] = str(getattr(cell, "text", "") or "")
    return output, list(dict.fromkeys(reasons))


def _column_boundaries(item: object, page_width: float, columns: int) -> list[float]:
    estimates: dict[int, list[float]] = {index: [] for index in range(columns + 1)}
    for cell in getattr(getattr(item, "data", None), "table_cells", []) or []:
        bbox = getattr(cell, "bbox", None)
        if bbox is None:
            continue
        start = int(getattr(cell, "start_col_offset_idx", -1))
        end = int(getattr(cell, "end_col_offset_idx", -1))
        if 0 <= start <= columns:
            estimates[start].append(float(getattr(bbox, "l", 0.0)) / page_width)
        if 0 <= end <= columns:
            estimates[end].append(float(getattr(bbox, "r", page_width)) / page_width)
    return [
        round(sum(values) / len(values), 6) if values else round(index / max(1, columns), 6)
        for index, values in estimates.items()
    ]


def _table_cells(
    table_data: object,
    *,
    page_width: float,
    page_height: float,
) -> list[TableCellFragment]:
    result: list[TableCellFragment] = []
    for cell in getattr(table_data, "table_cells", []) or []:
        start_row = int(getattr(cell, "start_row_offset_idx", -1))
        end_row = int(getattr(cell, "end_row_offset_idx", -1))
        start_col = int(getattr(cell, "start_col_offset_idx", -1))
        end_col = int(getattr(cell, "end_col_offset_idx", -1))
        if start_row < 0 or start_col < 0 or end_row <= start_row or end_col <= start_col:
            continue
        normalized_bbox: tuple[float, float, float, float] | None = None
        bbox = getattr(cell, "bbox", None)
        if bbox is not None and page_width > 0 and page_height > 0:
            try:
                top_left = bbox.to_top_left_origin(page_height=page_height)
                normalized_bbox = (
                    max(0.0, min(1.0, float(top_left.l) / page_width)),
                    max(0.0, min(1.0, float(top_left.t) / page_height)),
                    max(0.0, min(1.0, float(top_left.r) / page_width)),
                    max(0.0, min(1.0, float(top_left.b) / page_height)),
                )
            except (AttributeError, TypeError, ValueError):
                normalized_bbox = None
        result.append(
            TableCellFragment(
                row=start_row,
                column=start_col,
                row_span=end_row - start_row,
                column_span=end_col - start_col,
                text=str(getattr(cell, "text", "") or ""),
                normalized_bbox=normalized_bbox,
                is_column_header=bool(getattr(cell, "column_header", False)),
                is_row_header=bool(getattr(cell, "row_header", False)),
            )
        )
    return result


def extract_table_fragments(document: Any, page_group: tuple[int, int]) -> list[TableFragment]:
    iterator = getattr(document, "iterate_items", None)
    pages = getattr(document, "pages", None)
    if not callable(iterator) or not isinstance(pages, dict):
        return []
    try:
        from docling_core.types.doc import TableItem
    except ImportError:
        return []
    ordinals: dict[int, int] = {}
    fragments: list[TableFragment] = []
    for item, _level in iterator():
        if not isinstance(item, TableItem) or not item.prov:
            continue
        provenance = item.prov[0]
        page_number = provenance.page_no
        if not isinstance(page_number, int) or not page_group[0] <= page_number <= page_group[1]:
            continue
        ordinal = ordinals.get(page_number, 0) + 1
        ordinals[page_number] = ordinal
        page = pages.get(page_number)
        size = getattr(page, "size", None)
        width = float(getattr(size, "width", 0.0) or 0.0)
        height = float(getattr(size, "height", 0.0) or 0.0)
        if width <= 0 or height <= 0:
            continue
        try:
            bbox = provenance.bbox.to_top_left_origin(page_height=height)
            normalized_bbox = (
                max(0.0, min(1.0, float(bbox.l) / width)),
                max(0.0, min(1.0, float(bbox.t) / height)),
                max(0.0, min(1.0, float(bbox.r) / width)),
                max(0.0, min(1.0, float(bbox.b) / height)),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        rows, reasons = _anchored_rows(item.data)
        markdown = render_gfm_rows(rows)
        if reasons or not markdown:
            try:
                markdown = str(item.export_to_markdown(document) or "")
            except (AttributeError, TypeError, ValueError):
                markdown = ""
        fragment_id = f"p{page_number}_t{ordinal}"
        marker = f"<!-- table-fragment: {fragment_id} -->"
        rendered = f"{marker}\n\n{markdown}" if markdown else marker
        fragments.append(
            TableFragment(
                fragment_id=fragment_id,
                page_number=page_number,
                ordinal=ordinal,
                normalized_bbox=normalized_bbox,
                num_rows=int(item.data.num_rows),
                num_cols=int(item.data.num_cols),
                rows=rows,
                column_boundaries=_column_boundaries(item, width, int(item.data.num_cols)),
                markdown=markdown,
                rendered=rendered,
                valid=not reasons,
                invalid_reasons=reasons,
                has_column_header=any(
                    bool(getattr(cell, "column_header", False))
                    and int(getattr(cell, "start_row_offset_idx", -1)) == 0
                    for cell in item.data.table_cells
                ),
                source_kind="docling",
                cells=_table_cells(item.data, page_width=width, page_height=height),
            )
        )
    return fragments


def export_page_markdown(document: Any, page_number: int, fragments: list[TableFragment]) -> str:
    rendered = {fragment.fragment_id: fragment.rendered for fragment in fragments}
    refs: dict[str, str] = {}
    iterator = getattr(document, "iterate_items", None)
    if callable(iterator):
        try:
            from docling_core.types.doc import TableItem

            page_fragments = iter(
                fragment for fragment in fragments if fragment.page_number == page_number
            )
            for item, _level in iterator(page_no=page_number):
                if isinstance(item, TableItem):
                    fragment = next(page_fragments, None)
                    if fragment is not None:
                        refs[item.self_ref] = fragment.fragment_id
        except (ImportError, TypeError):
            refs = {}
    if not refs:
        return str(document.export_to_markdown(page_no=page_number) or "")
    try:
        from docling_core.transforms.serializer.base import BaseTableSerializer
        from docling_core.transforms.serializer.common import create_ser_result
        from docling_core.transforms.serializer.markdown import (
            MarkdownDocSerializer,
            MarkdownParams,
        )
        from pydantic import BaseModel, ConfigDict, Field

        class AnchoredTableSerializer(BaseModel, BaseTableSerializer):
            model_config = ConfigDict(arbitrary_types_allowed=True)

            refs: dict[str, str] = Field(default_factory=dict)
            rendered: dict[str, str] = Field(default_factory=dict)

            def serialize(self, *, item: Any, doc_serializer: Any, doc: Any, **kwargs: Any) -> Any:
                parts = []
                caption = doc_serializer.serialize_captions(item=item, **kwargs)
                if caption.text:
                    parts.append(caption)
                fragment_id = self.refs.get(item.self_ref)
                text = self.rendered.get(fragment_id or "", "")
                if text:
                    parts.append(create_ser_result(text=text, span_source=item))
                return create_ser_result(
                    text="\n\n".join(part.text for part in parts), span_source=parts
                )

        serializer = MarkdownDocSerializer(
            doc=document,
            params=MarkdownParams(pages={page_number}),
            table_serializer=AnchoredTableSerializer(refs=refs, rendered=rendered),
        )
        return str(serializer.serialize().text or "")
    except (ImportError, TypeError, ValueError):
        return str(document.export_to_markdown(page_no=page_number) or "")


def _headers_match(left: TableFragment, right: TableFragment) -> bool:
    if right.has_column_header and left.header_signature and right.header_signature:
        return SequenceMatcher(None, left.header_signature, right.header_signature).ratio() >= 0.80
    return bool(not right.has_column_header and left.num_cols == right.num_cols and right.rows)


def _columns_match(left: TableFragment, right: TableFragment) -> bool:
    if left.num_cols != right.num_cols or len(left.column_boundaries) != len(
        right.column_boundaries
    ):
        return False
    return (
        sum(
            abs(a - b) for a, b in zip(left.column_boundaries, right.column_boundaries, strict=True)
        )
        / max(1, len(left.column_boundaries))
        <= 0.02
    )


def _repair_continuation_shapes(
    pages: list[PageParseResult],
    fragments: list[TableFragment],
) -> None:
    """Expand a continuation table only when its compact header proves omitted blank columns."""

    by_page: dict[int, list[TableFragment]] = {}
    for fragment in fragments:
        by_page.setdefault(fragment.page_number, []).append(fragment)
    page_models = {page.page_number: page for page in pages}
    for page_number in sorted(by_page):
        left = sorted(by_page[page_number], key=lambda item: item.ordinal)[-1]
        following = sorted(by_page.get(page_number + 1, []), key=lambda item: item.ordinal)
        if not following:
            continue
        right = following[0]
        if (
            not left.valid
            or not right.valid
            or left.normalized_bbox[3] < 0.85
            or right.normalized_bbox[1] > 0.15
            or left.num_cols <= right.num_cols
            or not left.rows
            or not right.rows
            or len(left.column_boundaries) < 2
            or len(right.column_boundaries) < 2
        ):
            continue
        blank_columns = [index for index, cell in enumerate(left.rows[0]) if not cell.strip()]
        if left.num_cols - right.num_cols != len(blank_columns) or not blank_columns:
            continue
        left_header = "|".join(
            re.sub(r"\s+", "", cell).casefold() for cell in left.rows[0] if cell.strip()
        )
        right_header = "|".join(
            re.sub(r"\s+", "", cell).casefold() for cell in right.rows[0] if cell.strip()
        )
        if (
            not left_header
            or not right_header
            or SequenceMatcher(None, left_header, right_header).ratio() < 0.80
        ):
            continue
        outer_boundary_difference = (
            abs(left.column_boundaries[0] - right.column_boundaries[0])
            + abs(left.column_boundaries[-1] - right.column_boundaries[-1])
        ) / 2
        if outer_boundary_difference > 0.02:
            continue
        expanded: list[list[str]] = []
        for row in right.rows:
            output = list(row)
            for index in blank_columns:
                output.insert(index, "")
            if len(output) != left.num_cols:
                expanded = []
                break
            expanded.append(output)
        if not expanded:
            continue
        old_rendered = right.rendered
        right.rows = expanded
        right.num_cols = left.num_cols
        right.column_boundaries = list(left.column_boundaries)
        right.has_column_header = True
        right.markdown = render_gfm_rows(expanded)
        marker = f"<!-- table-fragment: {right.fragment_id} -->"
        right.rendered = f"{marker}\n\n{right.markdown}"
        page = page_models.get(right.page_number)
        if page is not None and page.content:
            page.content = page.content.replace(old_rendered, right.rendered, 1)


def link_cross_page_tables(fragments: list[TableFragment]) -> list[list[TableFragment]]:
    by_page: dict[int, list[TableFragment]] = {}
    for fragment in fragments:
        by_page.setdefault(fragment.page_number, []).append(fragment)
    groups: list[list[TableFragment]] = []
    consumed: set[str] = set()
    for page_number in sorted(by_page):
        previous = sorted(by_page[page_number], key=lambda item: item.ordinal)[-1]
        next_items = sorted(by_page.get(page_number + 1, []), key=lambda item: item.ordinal)
        if not next_items:
            continue
        following = next_items[0]
        if (
            previous.fragment_id not in consumed
            and following.fragment_id not in consumed
            and previous.valid
            and following.valid
            and previous.normalized_bbox[3] >= 0.85
            and following.normalized_bbox[1] <= 0.15
            and _columns_match(previous, following)
            and _headers_match(previous, following)
        ):
            group = next(
                (item for item in groups if item[-1].fragment_id == previous.fragment_id), None
            )
            if group is None:
                group = [previous]
                groups.append(group)
            group.append(following)
            consumed.update({previous.fragment_id, following.fragment_id})
    return groups


def _merged_rows(group: list[TableFragment]) -> list[list[str]]:
    rows = [row[:] for row in group[0].rows]
    header = group[0].header_signature
    for fragment in group[1:]:
        continuation = fragment.rows
        if (
            continuation
            and fragment.has_column_header
            and fragment.header_signature
            and SequenceMatcher(None, header, fragment.header_signature).ratio() >= 0.80
        ):
            continuation = continuation[1:]
        rows.extend(row[:] for row in continuation)
    return rows


def assemble_logical_tables(
    pages: list[PageParseResult],
    fragments: list[TableFragment],
    *,
    enabled: bool,
) -> dict[int, str]:
    _repair_continuation_shapes(pages, fragments)
    content_by_page = {page.page_number: page.content or "" for page in pages}
    page_models = {page.page_number: page for page in pages}
    if not fragments:
        return content_by_page
    for fragment in fragments:
        page = page_models.get(fragment.page_number)
        if page is not None:
            page.diagnostics = page.diagnostics or PageDiagnostics(source_kind=PageSourceKind.MIXED)
            if fragment.fragment_id not in page.diagnostics.logical_table_ids:
                page.diagnostics.logical_table_ids.append(fragment.fragment_id)
    if not enabled:
        return content_by_page
    for group in link_cross_page_tables(fragments):
        blocked_by_heading = False
        for left, right in zip(group, group[1:], strict=False):
            left_tail = content_by_page[left.page_number].partition(left.rendered)[2]
            right_head = content_by_page[right.page_number].partition(right.rendered)[0]
            if re.search(
                r"(?m)^(?:#{1,6}\s+\S|第[一二三四五六七八九十百0-9]+[章节]\s*|\d+(?:\.\d+)+\s+\S)",
                f"{left_tail}\n{right_head}",
            ):
                blocked_by_heading = True
                break
        if blocked_by_heading:
            continue
        logical_id = f"table_{group[0].fragment_id}_{group[-1].fragment_id}"
        source_pages = sorted({fragment.page_number for fragment in group})
        merged = render_gfm_rows(_merged_rows(group))
        logical = f"<!-- logical-table: {logical_id}; source-pages: {','.join(map(str, source_pages))} -->\n\n{merged}"
        first = group[0]
        content_by_page[first.page_number] = content_by_page[first.page_number].replace(
            first.rendered, logical, 1
        )
        for fragment in group:
            fragment.logical_table_id = logical_id
            page = page_models.get(fragment.page_number)
            if page and page.diagnostics:
                page.diagnostics.logical_table_ids = [
                    logical_id if item == fragment.fragment_id else item
                    for item in page.diagnostics.logical_table_ids
                ]
        for fragment in group[1:]:
            content_by_page[fragment.page_number] = content_by_page[fragment.page_number].replace(
                fragment.rendered, "", 1
            )
    return content_by_page
