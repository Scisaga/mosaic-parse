from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from pydantic import ValidationError

from app.models import (
    ContentParseOptions,
    DocumentParseResult,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    ParsePipeline,
    ParseProfile,
    SelectionStrategy,
    StoredSource,
    VlmPolicy,
)
from app.parsers.docling_standard import DoclingStandardParser
from app.parsers.ollama_vlm import OllamaVisualAdapter
from app.services.evidence_service import NativeBlock, PageEvidence, PageEvidenceService
from app.services.parser_service import ParserService
from app.services.quality_service import QualityService
from app.services.table_service import (
    TableFragment,
    _anchored_rows,
    assemble_logical_tables,
)


def _result(page: PageParseResult) -> DocumentParseResult:
    return DocumentParseResult(
        document_id="docparse_quality",
        filename="quality.pdf",
        mime_type="application/pdf",
        page_count=1,
        processed_pages=1,
        pages=[page],
        pipeline=ParsePipeline(profile="balanced", primary="docling-standard"),
    )


def test_visual_policy_is_derived_only_from_profile() -> None:
    assert (
        ContentParseOptions(profile=ParseProfile.ACCURATE).resolved_vlm_policy
        == VlmPolicy.AUTO_VISUAL
    )
    assert ContentParseOptions(profile=ParseProfile.BALANCED).resolved_vlm_policy == VlmPolicy.OFF
    with pytest.raises(ValidationError):
        ContentParseOptions.model_validate({"profile": "accurate", "vlm_policy": "off"})


def test_sparse_evidence_is_measured_and_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "sparse.pdf"
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 400), "续页。", fontsize=8)
    document.save(path)
    document.close()
    source = StoredSource(
        path=path,
        filename=path.name,
        mime_type="application/pdf",
        size_bytes=path.stat().st_size,
        page_count=1,
    )

    evidence = PageEvidenceService().inspect(source, {1})[1]

    assert evidence.source_kind == PageSourceKind.SPARSE
    assert evidence.native_text_characters is not None
    assert evidence.native_text_characters < 40
    assert evidence.visual_ink_ratio is not None
    assert evidence.visual_ink_ratio < 0.015


def test_native_reading_order_repair_requires_exact_multiset() -> None:
    lines = ["第一段唯一锚点甲乙", "第二段唯一锚点丙丁", "第三段唯一锚点戊己", "第四段唯一锚点庚辛"]
    blocks = [
        NativeBlock(
            text=line, bbox=(10, index * 20, 200, index * 20 + 10), max_font_size=10, bold=False
        )
        for index, line in enumerate(lines)
    ]
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_body_text="\n".join(lines),
        native_lines=lines,
        native_blocks=blocks,
        page_width=300,
    )
    page = PageParseResult(
        page_number=1,
        content="\n\n".join([lines[1], lines[0], lines[2], lines[3]]),
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)
    service = ParserService(SimpleNamespace(parser_workers=1))

    service._repair_native_reading_order(result, {1: evidence})

    assert result.pages[0].content == "\n\n".join(lines)
    assert result.pages[0].diagnostics is not None
    assert result.pages[0].diagnostics.selected_strategy == SelectionStrategy.NATIVE_REPAIR

    rejected = PageParseResult(
        page_number=1,
        content="\n\n".join([lines[1], lines[0], lines[2], lines[3]]) + "新增",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    rejected_result = _result(rejected)
    service._repair_native_reading_order(rejected_result, {1: evidence})
    assert rejected_result.pages[0].content.endswith("新增")
    assert rejected_result.pages[0].warnings[0].code == "reading_order_inversion"


def test_wingdings_mapping_requires_font_evidence() -> None:
    mapped = PageEvidenceService._block_text(
        {"lines": [{"spans": [{"text": "\uf052", "font": "Wingdings 2", "size": 10}]}]}
    )
    unmapped = PageEvidenceService._block_text(
        {"lines": [{"spans": [{"text": "\uf052", "font": "Arial", "size": 10}]}]}
    )
    assert mapped[3] == {"\uf052": "☑"}
    assert unmapped[3] == {}


def test_native_order_gate_accounts_for_proven_glyph_mapping() -> None:
    native_lines = [
        "第一段唯一锚点甲乙",
        "第二段唯一锚点丙丁",
        "第三段□适用\uf052不适用",
        "第四段唯一锚点庚辛",
    ]
    blocks = [
        NativeBlock(line, (10, index * 20, 200, index * 20 + 10), 10, False)
        for index, line in enumerate(native_lines)
    ]
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_lines=native_lines,
        native_blocks=blocks,
        glyph_mappings={"\uf052": "☑"},
        page_width=300,
        page_height=100,
    )
    rendered = [line.replace("\uf052", "☑") for line in native_lines]
    page = PageParseResult(
        page_number=1,
        content="\n\n".join([rendered[1], rendered[0], rendered[2], rendered[3]]),
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)

    ParserService(SimpleNamespace(parser_workers=1))._repair_native_reading_order(
        result, {1: evidence}
    )

    assert result.pages[0].content == "\n\n".join(rendered)


def test_native_order_repair_matches_repeated_identical_blocks_by_occurrence() -> None:
    lines = [
        "第一段唯一锚点甲乙",
        "第二段唯一锚点丙丁",
        "重复选择内容",
        "第三段唯一锚点戊己",
        "重复选择内容",
        "第四段唯一锚点庚辛",
    ]
    blocks = [
        NativeBlock(line, (10, index * 15, 200, index * 15 + 10), 10, False)
        for index, line in enumerate(lines)
    ]
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_lines=lines,
        native_blocks=blocks,
        page_width=300,
        page_height=100,
    )
    page = PageParseResult(
        page_number=1,
        content="\n\n".join([lines[1], lines[0], *lines[2:]]),
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)

    ParserService(SimpleNamespace(parser_workers=1))._repair_native_reading_order(
        result, {1: evidence}
    )

    assert result.pages[0].content == "\n\n".join(lines)


def test_span_export_anchors_merged_value_and_rejects_overlap() -> None:
    cells = [
        SimpleNamespace(
            start_row_offset_idx=0,
            end_row_offset_idx=1,
            start_col_offset_idx=0,
            end_col_offset_idx=2,
            text="合并表头",
        ),
        SimpleNamespace(
            start_row_offset_idx=1,
            end_row_offset_idx=2,
            start_col_offset_idx=0,
            end_col_offset_idx=1,
            text="A",
        ),
        SimpleNamespace(
            start_row_offset_idx=1,
            end_row_offset_idx=2,
            start_col_offset_idx=1,
            end_col_offset_idx=2,
            text="B",
        ),
    ]
    rows, reasons = _anchored_rows(SimpleNamespace(num_rows=2, num_cols=2, table_cells=cells))
    assert reasons == []
    assert rows == [["合并表头", ""], ["A", "B"]]

    cells.append(cells[-1])
    _rows, reasons = _anchored_rows(SimpleNamespace(num_rows=2, num_cols=2, table_cells=cells))
    assert reasons == ["overlapping_cells"]


def _fragment(
    fragment_id: str,
    page_number: int,
    bbox: tuple[float, float, float, float],
    rows: list[list[str]],
) -> TableFragment:
    markdown = "\n".join(
        [
            "| " + " | ".join(rows[0]) + " |",
            "| " + " | ".join("---" for _ in rows[0]) + " |",
            *("| " + " | ".join(row) + " |" for row in rows[1:]),
        ]
    )
    return TableFragment(
        fragment_id=fragment_id,
        page_number=page_number,
        ordinal=1,
        normalized_bbox=bbox,
        num_rows=len(rows),
        num_cols=len(rows[0]),
        rows=rows,
        column_boundaries=[0.1, 0.5, 0.9],
        markdown=markdown,
        rendered=f"<!-- table-fragment: {fragment_id} -->\n\n{markdown}",
        has_column_header=True,
    )


def test_native_repair_keeps_table_region_unchanged() -> None:
    lines = ["第一节唯一锚点甲乙", "第二节唯一锚点丙丁", "第三节唯一锚点戊己", "第四节唯一锚点庚辛"]
    table = _fragment("p1_t1", 1, (0.1, 0.3, 0.9, 0.55), [["项目", "金额"], ["现金", "100"]])
    blocks = [
        NativeBlock(text=lines[0], bbox=(10, 5, 90, 15), max_font_size=10, bold=False),
        NativeBlock(text=lines[1], bbox=(10, 20, 90, 30), max_font_size=10, bold=False),
        NativeBlock(text="项目 金额 现金 100", bbox=(10, 32, 90, 52), max_font_size=10, bold=False),
        NativeBlock(text=lines[2], bbox=(10, 60, 90, 70), max_font_size=10, bold=False),
        NativeBlock(text=lines[3], bbox=(10, 75, 90, 85), max_font_size=10, bold=False),
    ]
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_lines=lines,
        native_blocks=blocks,
        page_width=100,
        page_height=100,
    )
    page = PageParseResult(
        page_number=1,
        content="\n\n".join([lines[1], lines[0], table.rendered, lines[2], lines[3]]),
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)
    result._table_fragments = [table]

    ParserService(SimpleNamespace(parser_workers=1))._repair_native_reading_order(
        result, {1: evidence}
    )

    assert result.pages[0].content is not None
    assert result.pages[0].content.index(lines[0]) < result.pages[0].content.index(lines[1])
    assert result.pages[0].content.count(table.rendered) == 1


def test_cross_page_table_merges_only_canonical_content() -> None:
    header = ["项目", "金额"]
    first = _fragment("p1_t1", 1, (0.1, 0.7, 0.9, 0.96), [header, ["甲", "1"]])
    second = _fragment("p2_t1", 2, (0.1, 0.03, 0.9, 0.4), [header, ["乙", "2"]])
    pages = [
        PageParseResult(page_number=1, content=first.rendered),
        PageParseResult(page_number=2, content=second.rendered),
    ]

    canonical = assemble_logical_tables(pages, [first, second], enabled=True)

    assert "logical-table" in canonical[1]
    assert canonical[1].count("| 项目 | 金额 |") == 1
    assert "| 乙 | 2 |" in canonical[1]
    assert canonical[2] == ""
    assert pages[0].content == first.rendered
    assert pages[1].content == second.rendered
    assert pages[0].diagnostics is not None
    assert pages[0].diagnostics.logical_table_ids == ["table_p1_t1_p2_t1"]


def test_cross_page_continuation_expands_proven_blank_column() -> None:
    first = _fragment(
        "p1_t1",
        1,
        (0.15, 0.7, 0.85, 0.96),
        [["项目", "", "序号", "金额"], ["投入", "", "A", "1"]],
    )
    first.column_boundaries = [0.15, 0.32, 0.46, 0.70, 0.84]
    second = _fragment(
        "p2_t1",
        2,
        (0.15, 0.03, 0.84, 0.2),
        [["项", "序号", "金额"], ["差异", "G=E-F", "30,000.00"]],
    )
    second.has_column_header = False
    second.column_boundaries = [0.16, 0.38, 0.68, 0.82]
    pages = [
        PageParseResult(page_number=1, content=first.rendered),
        PageParseResult(page_number=2, content=second.rendered),
    ]

    canonical = assemble_logical_tables(pages, [first, second], enabled=True)

    assert "logical-table" in canonical[1]
    assert "| 差异 |  | G=E-F | 30,000.00 |" in canonical[1]
    assert canonical[2] == ""
    assert "| 差异 |  | G=E-F | 30,000.00 |" in (pages[1].content or "")


def test_directory_page_becomes_list_not_table() -> None:
    page = PageParseResult(page_number=1, content="| 目录 | 页码 |")
    result = _result(page)
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_lines=["目录", "第一章........1", "第二章........3", "释义........5"],
    )
    ParserService._normalize_directory_pages(result, {1: evidence})
    assert result.pages[0].content == "# 目录\n\n- 第一章 …… 1\n- 第二章 …… 3\n- 释义 …… 5"

    result.pages[0].diagnostics = PageDiagnostics(
        source_kind=PageSourceKind.NATIVE,
        selected_strategy=SelectionStrategy.NATIVE_REPAIR,
        native_text_characters=1_000,
    )
    assert not any(
        warning.code == "visual_text_mismatch"
        for warning in QualityService().inspect_page(result.pages[0])
    )


def test_native_line_evidence_rejoins_split_heading() -> None:
    page = PageParseResult(
        page_number=1,
        content="## 1\n\n##、项目立项审批\n\n正文",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_lines=["1、项目立项审批", "正文"],
    )

    ParserService._repair_split_headings(result, {1: evidence})

    assert result.pages[0].content == "## 1、项目立项审批\n\n正文"
    assert result.pages[0].diagnostics is not None
    assert result.pages[0].diagnostics.selected_strategy == SelectionStrategy.NATIVE_REPAIR


def test_position_evidence_removes_only_standalone_table_number() -> None:
    table = _fragment(
        "p1_t1",
        1,
        (0.1, 0.3, 0.9, 0.8),
        [["项目", "金额"], ["利息", "813.01"]],
    )
    page = PageParseResult(
        page_number=1,
        content=f"说明\n\n813.01\n\n{table.rendered}",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
    )
    result = _result(page)
    result._table_fragments = [table]
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.NATIVE,
        native_blocks=[
            NativeBlock("说明", (10, 10, 50, 20), 10, False),
            NativeBlock("利息\n813.01", (10, 40, 90, 60), 10, False),
        ],
        page_width=100,
        page_height=100,
    )

    ParserService._remove_unanchored_table_numbers(result, {1: evidence})

    assert result.pages[0].content.count("813.01") == 1
    assert result.pages[0].diagnostics is not None
    assert result.pages[0].diagnostics.selected_strategy == SelectionStrategy.NATIVE_REPAIR


def test_table_fragment_reference_refreshes_after_text_normalization() -> None:
    table = _fragment("p1_t1", 1, (0.1, 0.3, 0.9, 0.8), [["项目", "金额"], ["总 资产", "1"]])
    page = PageParseResult(
        page_number=1,
        content=table.rendered.replace("总 资产", "总资产"),
    )
    result = _result(page)
    result._table_fragments = [table]

    ParserService._refresh_table_fragment_renderings(result)

    assert "总资产" in table.rendered
    assert table.rendered in (page.content or "")


def test_repeated_url_prefix_is_not_repeated_text_corruption() -> None:
    rows = "\n".join(
        f"| 公告 {index} | 巨潮资讯网（http://www.cninfo.com.cn） |" for index in range(8)
    )
    page = PageParseResult(
        page_number=1,
        content=f"| 项目 | 链接 |\n| --- | --- |\n{rows}",
    )

    codes = {warning.code for warning in QualityService().inspect_page(page)}

    assert "repeated_text" not in codes
    assert "unanchored_table_numbers" not in codes


def test_repeated_image_placeholders_are_not_text_corruption() -> None:
    page = PageParseResult(
        page_number=1,
        content="\n\n".join(["签章视觉证据", *(["<!-- image -->"] * 6)]),
    )

    codes = {warning.code for warning in QualityService().inspect_page(page)}

    assert "repeated_text" not in codes


def test_repeated_not_applicable_cells_are_not_header_propagation() -> None:
    page = PageParseResult(
        page_number=1,
        content=(
            "| 项目 | A | B | C | D | E |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 项目二 | 不适用 | 不适用 | 不适用 | 不适用 | 不适用 |"
        ),
    )

    codes = {warning.code for warning in QualityService().inspect_page(page)}

    assert "table_header_propagation" not in codes


def test_repeated_financial_values_are_not_header_propagation() -> None:
    page = PageParseResult(
        page_number=1,
        content=(
            "| 项目 | 承诺 | 调整 | 实际 | 差额 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| 补充流动资金 | 250,000,000.00 | 250,000,000.00 | "
            "250,000,000.00 | 0.00 |"
        ),
    )

    codes = {warning.code for warning in QualityService().inspect_page(page)}

    assert "table_header_propagation" not in codes


def test_visual_trigger_requires_measured_scan_grid() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1))
    page = PageParseResult(
        page_number=1,
        content="OCR text without a table",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.SCANNED),
    )
    result = _result(page)
    evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.SCANNED,
        horizontal_grid_lines=3,
        vertical_grid_lines=3,
        grid_area_ratio=0.25,
    )
    assert service._complex_visual_targets(result, {1: evidence}) == {1: "table"}
    evidence.grid_area_ratio = 0.249
    assert service._complex_visual_targets(result, {1: evidence}) == {}


def test_rotation_bbox_is_deterministic() -> None:
    assert OllamaVisualAdapter._rotate_bbox((0.1, 0.2, 0.4, 0.6), 90) == (0.4, 0.1, 0.8, 0.4)


def test_overlapping_ocr_dedup_requires_same_text_and_iou() -> None:
    def item(text: str, bbox: tuple[int, int, int, int]) -> SimpleNamespace:
        return SimpleNamespace(
            text=text,
            prov=[
                SimpleNamespace(
                    page_no=1,
                    bbox=SimpleNamespace(l=bbox[0], t=bbox[1], r=bbox[2], b=bbox[3]),
                )
            ],
        )

    items = [
        item("张三", (0, 0, 10, 10)),
        item("张三", (0, 0, 10, 10)),
        item("张三", (30, 30, 40, 40)),
        item("李四", (0, 0, 10, 10)),
    ]
    document = SimpleNamespace(iterate_items=lambda: ((value, 0) for value in items))
    duplicates = DoclingStandardParser._overlapping_text_duplicate_counts(document, (1, 1))
    assert duplicates == {1: Counter({"张三": 1})}
    assert (
        DoclingStandardParser._remove_overlapping_text_duplicates(
            "张三\n张三\n张三\n李四", duplicates[1]
        )
        == "张三\n张三\n李四"
    )
