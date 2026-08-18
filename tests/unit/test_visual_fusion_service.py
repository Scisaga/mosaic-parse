from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image
from pydantic import ValidationError

from app.models import (
    ContentParseOptions,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    SelectionStrategy,
)
from app.parsers.ollama_vlm import StructuredVlmCompletion, VlmResponseTruncatedError
from app.services.evidence_service import PageEvidence
from app.services.table_service import TableFragment, render_gfm_rows
from app.services.visual_fusion_service import (
    VisualBandExtraction,
    VisualConflictBatch,
    VisualConflictResolution,
    VisualFusionService,
    VisualParallelBandExtraction,
    VisualRowExtraction,
    VisualSignatureExtraction,
    VisualTableExtraction,
)


def _png(width: int = 600, height: int = 800) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, format="PNG")
    return output.getvalue()


def _completion(value):
    return StructuredVlmCompletion(
        value=value,
        duration_ms=5,
        finish_reason="stop",
        reasoning_characters=10,
        prompt_tokens=20,
        completion_tokens=30,
    )


class _Qwen:
    name = "ollama-vlm"

    def __init__(self, band: VisualBandExtraction, conflict: VisualConflictBatch | None = None):
        self.band = band
        self.conflict = conflict
        self.calls: list[type] = []
        self.reasoning_efforts: list[str] = []

    async def _render(self, source, page_number, profile):
        return _png()

    async def complete_structured(self, images, prompt, schema, *, max_tokens, reasoning_effort):
        self.calls.append(schema)
        self.reasoning_efforts.append(reasoning_effort)
        if schema is VisualConflictBatch:
            return _completion(self.conflict or VisualConflictBatch())
        return _completion(self.band)

    @staticmethod
    def _rotate_image(image, degrees):
        return image

    @staticmethod
    def _rotate_bbox(bbox, degrees):
        return bbox


def _fragment(value: str = "100") -> TableFragment:
    rows = [["项目", "金额"], ["现金", value]]
    markdown = render_gfm_rows(rows)
    return TableFragment(
        fragment_id="sdk_p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.05, 0.2, 0.95, 0.8),
        num_rows=2,
        num_cols=2,
        rows=rows,
        column_boundaries=[0.05, 0.5, 0.95],
        markdown=markdown,
        rendered=f"<!-- table-fragment: sdk_p1_t1 -->\n\n{markdown}",
        has_column_header=True,
    )


def _page(fragment: TableFragment) -> PageParseResult:
    return PageParseResult(
        page_number=1,
        content=f"保留的非表正文\n\n{fragment.rendered}\n\n<!-- image -->",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.SCANNED),
    )


def _evidence(*, rows: int = 4, columns: int = 4) -> PageEvidence:
    return PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.SCANNED,
        detected_rotation_degrees=0,
        horizontal_grid_lines=rows,
        vertical_grid_lines=columns,
        grid_area_ratio=0.5,
        grid_regions=[(0.05, 0.2, 0.95, 0.8)],
    )


def test_parallel_band_schema_requires_exact_left_and_right_pair() -> None:
    table = {
        "table_id": "single",
        "columns": ["项目", "金额"],
        "rows": [{"kind": "data", "cells": ["现金", "100"]}],
    }

    with pytest.raises(ValidationError):
        VisualParallelBandExtraction.model_validate({"tables": [table]})

    parsed = VisualParallelBandExtraction.model_validate(
        {
            "tables": [
                {**table, "table_id": "left"},
                {**table, "table_id": "right"},
            ]
        }
    )
    assert [item.table_id for item in parsed.tables] == ["left", "right"]


async def test_region_fusion_preserves_non_table_content_and_resolves_cells() -> None:
    fragment = _fragment("100")
    band = VisualBandExtraction(
        company_name="测试新材料股份有限公司",
        statement_date="2026年6月30日",
        tables=[
            VisualTableExtraction(
                table_id="single",
                title="资产负债表",
                columns=["项目", "金额"],
                rows=[VisualRowExtraction(kind="data", cells=["现金", "999"])],
            )
        ],
    )
    qwen = _Qwen(
        band,
        VisualConflictBatch(
            resolutions=[VisualConflictResolution(conflict_id="t0r0c1", observed_value="100")]
        ),
    )
    service = VisualFusionService(
        SimpleNamespace(vlm_max_calls_per_page=3, vlm_page_budget_seconds=180), qwen
    )

    primary = _page(fragment)
    primary.content = f"测试通材料股份有限公司\n\n{primary.content}"
    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        primary,
        _evidence(),
        None,
        [fragment],
        [],
    )

    assert "保留的非表正文" in (outcome.page.content or "")
    assert "<!-- image -->" in (outcome.page.content or "")
    assert "测试新材料股份有限公司" in (outcome.page.content or "")
    assert "测试通材料股份有限公司" not in (outcome.page.content or "")
    assert "2026年6月30日" in (outcome.page.content or "")
    assert "| 现金 | 100 |" in (outcome.page.content or "")
    assert outcome.ir.company_name == "测试新材料股份有限公司"
    assert outcome.ir.statement_date == "2026年6月30日"
    assert outcome.page.diagnostics is not None
    assert outcome.page.diagnostics.selected_strategy == SelectionStrategy.QWEN_VISUAL_FUSION
    assert outcome.page.diagnostics.visual_fusion is not None
    assert outcome.page.diagnostics.visual_fusion.qwen_calls == 2
    assert outcome.page.diagnostics.visual_fusion.qwen_resolved_conflicts == 1
    assert outcome.page.diagnostics.visual_fusion.unresolved_conflicts == 0
    assert qwen.reasoning_efforts == ["none", "medium"]


async def test_parallel_visual_tables_become_two_fragments() -> None:
    fragment = _fragment()
    band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="left",
                section_name="left",
                columns=["资产", "金额"],
                rows=[VisualRowExtraction(kind="data", cells=["现金", "100"])],
            ),
            VisualTableExtraction(
                table_id="right",
                section_name="right",
                columns=["负债", "金额"],
                rows=[VisualRowExtraction(kind="data", cells=["应付债券", "633,255,314.00"])],
            ),
        ]
    )
    service = VisualFusionService(SimpleNamespace(), _Qwen(band))

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(fragment),
        _evidence(rows=24, columns=8),
        None,
        [fragment],
        [],
    )

    assert len(outcome.fragments) == 2
    assert outcome.fragments[0].num_cols == 2
    assert outcome.fragments[1].rows[1] == ["应付债券", "633,255,314.00"]


async def test_wide_parallel_table_is_normalized_before_cross_band_grouping() -> None:
    fragment = _fragment()
    band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=["资产", "附注", "期末", "期初", "负债", "附注", "期末", "期初"],
                rows=[
                    VisualRowExtraction(
                        kind="data", cells=["现金", "", "100", "90", "应付债券", "", "80", "70"]
                    )
                ],
            )
        ]
    )
    service = VisualFusionService(SimpleNamespace(), _Qwen(band))

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(fragment),
        _evidence(rows=24, columns=8),
        None,
        [fragment],
        [],
    )

    assert len(outcome.fragments) == 2
    assert [fragment.num_cols for fragment in outcome.fragments] == [4, 4]
    assert outcome.fragments[0].rows[1][0] == "现金"
    assert outcome.fragments[1].rows[1][0] == "应付债券"


async def test_short_wide_table_remains_one_table() -> None:
    fragment = _fragment()
    band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=[f"列{index}" for index in range(8)],
                rows=[VisualRowExtraction(kind="data", cells=[str(index) for index in range(8)])],
            )
        ]
    )
    service = VisualFusionService(SimpleNamespace(), _Qwen(band))

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(fragment),
        _evidence(rows=8, columns=11),
        None,
        [fragment],
        [],
    )

    assert len(outcome.fragments) == 1
    assert outcome.fragments[0].num_cols == 8


def test_cell_ir_preserves_invisible_null_and_visible_empty() -> None:
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=["项目", "金额"],
                        rows=[
                            VisualRowExtraction(kind="data", cells=["不可见", None]),
                            VisualRowExtraction(kind="data", cells=["可见空白", ""]),
                        ],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    assert tables[0].evidence[0][1]["qwen"] is None
    assert tables[0].evidence[1][1]["qwen"] == ""


def test_total_row_with_blank_sequence_is_not_a_missing_data_label() -> None:
    table = VisualTableExtraction(
        table_id="single",
        columns=["序号", "项目", "金额"],
        rows=[
            VisualRowExtraction(kind="data", cells=["1", "项目一", "100"]),
            VisualRowExtraction(kind="data", cells=["", "合计", "100"]),
        ],
    )

    assert len(table.rows) == 2


def test_misclassified_financial_row_is_not_dropped_as_header() -> None:
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=["项目", "附注", "2026年6月30日", "2025年12月31日"],
                        rows=[
                            VisualRowExtraction(
                                kind="header",
                                cells=["项目", "附注", "2026年6月30日", "2025年12月31日"],
                            ),
                            VisualRowExtraction(
                                kind="header",
                                cells=["应付债券", "五、（二十八）", "633,255,314.00", ""],
                            ),
                        ],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    assert tables[0].rows == [["应付债券", "五、（二十八）", "633,255,314.00", ""]]


def test_partition_prompt_contains_no_cross_band_ocr_row_hints() -> None:
    prompt = VisualFusionService._table_prompt(
        band_index=1,
        band_count=3,
        parallel_hint=False,
        languages=["zh", "en"],
        locked_layout={"single": ["项目", "金额"]},
    )

    assert "应付债券" not in prompt
    assert "633,255,314.00" not in prompt
    assert "current table-band image" not in prompt
    assert "do not repeat rows from overlapping band margins" in prompt.lower()


def test_rotated_parallel_scan_drops_unlocalized_ocr_but_keeps_visual_evidence() -> None:
    source_fragment = _fragment()
    primary = _page(source_fragment)
    primary.content += "\n\n条材盒\n印唬"
    qwen_fragment = TableFragment(
        fragment_id="qwen_p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.05, 0.1, 0.95, 0.85),
        num_rows=2,
        num_cols=2,
        rows=[["项目", "金额"], ["现金", "100"]],
        column_boundaries=[0.05, 0.5, 0.95],
        markdown=render_gfm_rows([["项目", "金额"], ["现金", "100"]]),
        rendered=(
            "<!-- table-fragment: qwen_p1_t1 -->\n\n| 项目 | 金额 |\n| --- | --- |\n| 现金 | 100 |"
        ),
        has_column_header=True,
    )

    content = VisualFusionService._replace_table_regions(
        primary,
        None,
        [],
        [qwen_fragment],
        page_metadata=("测试股份有限公司", "资产负债表", "2026年6月30日", "元"),
        discard_unlocalized_scanned_text=True,
    )

    assert "测试股份有限公司" in content
    assert "| 现金 | 100 |" in content
    assert "<!-- image -->" in content
    assert "保留的非表正文" not in content
    assert "条材盒" not in content
    assert "印唬" not in content


def test_two_non_qwen_sources_override_a_disagreeing_qwen_value() -> None:
    fragment = _fragment("100")
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=["项目", "金额"],
                        rows=[VisualRowExtraction(kind="data", cells=["现金", "999"])],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    agreed, qwen_selected, conflicts = service._merge_sources(tables, [fragment], [fragment])

    assert tables[0].rows[0][1] == "100"
    assert agreed >= 2
    assert qwen_selected == 0
    assert conflicts == []


def test_glm_complete_row_supplements_qwen_omission_without_moving_its_value() -> None:
    rows = [
        ["项目", "附注", "金额"],
        ["现金", "五、（一）", "100.00"],
        ["应付债券", "五、（二十八）", "633,255,314.00"],
    ]
    fragment = TableFragment(
        fragment_id="sdk_p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.05, 0.2, 0.95, 0.8),
        num_rows=3,
        num_cols=3,
        rows=rows,
        column_boundaries=[0.05, 0.35, 0.65, 0.95],
        markdown=render_gfm_rows(rows),
        rendered="",
        has_column_header=True,
    )
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=rows[0],
                        rows=[VisualRowExtraction(kind="data", cells=rows[1])],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    service._merge_sources(tables, [fragment], [])

    assert tables[0].rows == rows[1:]
    assert tables[0].evidence[1][2] == {
        "qwen": None,
        "glm": "633,255,314.00",
        "docling": None,
        "final": "633,255,314.00",
    }


def test_overlapping_band_row_variant_uses_multi_source_supported_value() -> None:
    rows = [
        ["项目", "附注", "期末", "期初"],
        ["无形资产", "五、15", "352,457,398.31", "363,620,212.92"],
    ]
    fragment = TableFragment(
        fragment_id="sdk_p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.05, 0.2, 0.95, 0.8),
        num_rows=2,
        num_cols=4,
        rows=rows,
        column_boundaries=[0.05, 0.25, 0.5, 0.75, 0.95],
        markdown=render_gfm_rows(rows),
        rendered="",
        has_column_header=True,
    )
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=rows[0],
                        rows=[VisualRowExtraction(kind="data", cells=rows[1])],
                    )
                ]
            ),
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=rows[0],
                        rows=[
                            VisualRowExtraction(
                                kind="data",
                                cells=[
                                    "无形资产",
                                    "五、15",
                                    "352,457,398.31",
                                    "363,202,212.92",
                                ],
                            )
                        ],
                    )
                ]
            ),
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    service._merge_sources(tables, [fragment], [fragment])

    assert tables[0].rows == [rows[1]]


def test_nearby_same_label_rows_with_different_values_are_preserved() -> None:
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=["项目", "期末", "期初"],
                        rows=[
                            VisualRowExtraction(kind="data", cells=["其他", "100", "90"]),
                            VisualRowExtraction(kind="data", cells=["其他", "200", "180"]),
                        ],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    service._merge_sources(tables, [], [])

    assert tables[0].rows == [["其他", "100", "90"], ["其他", "200", "180"]]


def test_source_consensus_collapses_distant_band_repeat_after_value_selection() -> None:
    columns = ["项目", "附注", "期末", "期初"]
    correct = ["短期借款", "", "296,155,561.32", "323,988,595.63"]
    source = TableFragment(
        fragment_id="sdk_p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.05, 0.2, 0.95, 0.8),
        num_rows=2,
        num_cols=4,
        rows=[columns, correct],
        column_boundaries=[0.05, 0.25, 0.5, 0.75, 0.95],
        markdown=render_gfm_rows([columns, correct]),
        rendered="",
        has_column_header=True,
    )
    qwen_rows = [
        correct,
        *[[f"中间行{index}", "", str(index), ""] for index in range(7)],
        ["短期借款", "", "296,155,661.32", "323,988,595.63"],
    ]
    service = VisualFusionService(SimpleNamespace(), _Qwen(VisualBandExtraction()))
    tables = service._combine_bands(
        [
            VisualBandExtraction(
                tables=[
                    VisualTableExtraction(
                        table_id="single",
                        columns=columns,
                        rows=[VisualRowExtraction(kind="data", cells=row) for row in qwen_rows],
                    )
                ]
            )
        ],
        (0.0, 0.0, 1.0, 1.0),
    )

    service._merge_sources(tables, [source], [source])

    assert [row for row in tables[0].rows if row[0] == "短期借款"] == [correct]


class _TruncatingQwen(_Qwen):
    async def complete_structured(self, images, prompt, schema, *, max_tokens, reasoning_effort):
        self.calls.append(schema)
        if len(self.calls) == 1:
            raise VlmResponseTruncatedError("length")
        return _completion(self.band)


async def test_truncated_band_is_split_under_three_call_budget() -> None:
    fragment = _fragment()
    band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=["项目", "金额"],
                rows=[VisualRowExtraction(kind="data", cells=["现金", "100"])],
            )
        ]
    )
    qwen = _TruncatingQwen(band)
    service = VisualFusionService(
        SimpleNamespace(vlm_max_calls_per_page=3, vlm_page_budget_seconds=180), qwen
    )

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(fragment),
        _evidence(),
        None,
        [fragment],
        [],
    )

    diagnostics = outcome.page.diagnostics
    assert diagnostics is not None and diagnostics.visual_fusion is not None
    assert diagnostics.visual_fusion.qwen_calls == 3
    assert diagnostics.visual_fusion.truncated_calls == 1
    assert diagnostics.visual_fusion.partitions == 3
    assert any(warning.code == "qwen_response_truncated" for warning in outcome.page.warnings)


async def test_rotated_dense_table_reserves_one_call_for_conflict_resolution() -> None:
    fragment = _fragment()
    band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=["项目", "金额"],
                rows=[VisualRowExtraction(kind="data", cells=["现金", "100"])],
            )
        ]
    )
    qwen = _Qwen(band)
    service = VisualFusionService(
        SimpleNamespace(vlm_max_calls_per_page=3, vlm_page_budget_seconds=180), qwen
    )
    evidence = _evidence(rows=19, columns=61)
    evidence.detected_rotation_degrees = 90

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(fragment),
        evidence,
        None,
        [fragment],
        [],
    )

    diagnostics = outcome.page.diagnostics
    assert diagnostics is not None and diagnostics.visual_fusion is not None
    assert diagnostics.visual_fusion.qwen_calls == 2
    assert diagnostics.visual_fusion.partitions == 2
    assert qwen.reasoning_efforts == ["none", "none"]


class _SequentialBandQwen(_Qwen):
    def __init__(
        self,
        bands: list[VisualBandExtraction],
        conflict: VisualConflictBatch,
    ) -> None:
        super().__init__(bands[0], conflict)
        self.bands = list(bands)

    async def complete_structured(self, images, prompt, schema, *, max_tokens, reasoning_effort):
        self.calls.append(schema)
        self.reasoning_efforts.append(reasoning_effort)
        if schema is VisualConflictBatch:
            return _completion(self.conflict or VisualConflictBatch())
        return _completion(self.bands.pop(0))


async def test_dense_band_replay_is_removed_and_variant_is_sent_to_conflict_call() -> None:
    columns = ["项目", "附注", "期末", "期初"]
    first_band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=columns,
                rows=[
                    VisualRowExtraction(kind="data", cells=["短期借款", "一", "100", "90"]),
                    VisualRowExtraction(kind="data", cells=["应付票据", "二", "200", "180"]),
                    VisualRowExtraction(kind="data", cells=["应付账款", "三", "300", "280"]),
                    VisualRowExtraction(kind="section", cells=["非流动负债：", "", "", ""]),
                    VisualRowExtraction(
                        kind="data",
                        cells=["应付债券", "二十八", "633,255,344.00", ""],
                    ),
                ],
            )
        ]
    )
    second_band = VisualBandExtraction(
        tables=[
            VisualTableExtraction(
                table_id="single",
                columns=columns,
                rows=[
                    VisualRowExtraction(kind="data", cells=["短期借款", "一", "", ""]),
                    VisualRowExtraction(kind="data", cells=["应付票据", "二", "", ""]),
                    VisualRowExtraction(kind="data", cells=["应付账款", "三", "", ""]),
                    VisualRowExtraction(
                        kind="data",
                        cells=["应付债券", "二十八", "633,255,314.00", ""],
                    ),
                    VisualRowExtraction(kind="data", cells=["其中：优先股", "", "", ""]),
                ],
            )
        ]
    )
    qwen = _SequentialBandQwen(
        [first_band, second_band],
        VisualConflictBatch(
            company_name="江苏联瑞新材料股份有限公司",
            resolutions=[
                VisualConflictResolution(
                    conflict_id="t0r4c2",
                    observed_value="633,255,314.00",
                )
            ],
        ),
    )
    service = VisualFusionService(
        SimpleNamespace(vlm_max_calls_per_page=3, vlm_page_budget_seconds=180),
        qwen,
    )

    outcome = await service.fuse_table_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        _page(_fragment()),
        _evidence(rows=61, columns=4),
        None,
        [],
        [],
    )

    payable_rows = [row for row in outcome.fragments[0].rows if row[0] == "应付债券"]
    assert payable_rows == [["应付债券", "二十八", "633,255,314.00", ""]]
    assert "江苏联瑞新材料股份有限公司" in (outcome.page.content or "")
    assert all(
        sum(row[0] == label for row in outcome.fragments[0].rows) == 1
        for label in ("短期借款", "应付票据", "应付账款")
    )
    diagnostics = outcome.page.diagnostics
    assert diagnostics is not None and diagnostics.visual_fusion is not None
    assert diagnostics.visual_fusion.qwen_calls == 3
    assert diagnostics.visual_fusion.qwen_resolved_conflicts == 1
    assert diagnostics.visual_fusion.unresolved_conflicts == 0


async def test_signature_fusion_deduplicates_printed_name_and_keeps_visual_placeholder() -> None:
    signature = VisualSignatureExtraction(
        printed_lines=["法定代表人：张三", "一图", "董事长"],
        duplicate_printed_lines=["法定代表人：张三"],
        visual_only_names=[],
        seal_or_handwriting_ocr_line_ids=["l0", "l2", "l3"],
        has_seal=True,
        has_handwriting=True,
    )
    qwen = _Qwen(VisualBandExtraction())

    async def signature_completion(images, prompt, schema, *, max_tokens, reasoning_effort):
        qwen.calls.append(schema)
        return _completion(signature)

    qwen.complete_structured = signature_completion
    service = VisualFusionService(SimpleNamespace(), qwen)
    primary = PageParseResult(
        page_number=1,
        content="法定代表人：张三\n法定代表人：张三\n印章乱码\n一图\n三里庄\n董事长",
        diagnostics=PageDiagnostics(source_kind=PageSourceKind.SCANNED),
    )

    outcome = await service.fuse_signature_page(
        SimpleNamespace(),
        ContentParseOptions(profile="accurate"),
        primary,
        _evidence(),
        None,
    )

    assert (outcome.page.content or "").count("法定代表人：张三") == 1
    assert "印章乱码" not in (outcome.page.content or "")
    assert "一图" not in (outcome.page.content or "")
    assert "三里庄" not in (outcome.page.content or "")
    assert "董事长" in (outcome.page.content or "")
    assert "<!-- image -->" in (outcome.page.content or "")


def test_signature_line_deduplication_groups_minor_ocr_variants() -> None:
    content = (
        "安永华明会计师事务所（特殊普通合伙）\n"
        "安永明会计师事务所（特殊普通合伙）\n"
        "中国注册会计师：莫威威"
    )

    result = VisualFusionService._deduplicate_lines(
        content,
        ["安永华明会计师事务所（特殊普通合伙）"],
    )

    assert result == "安永华明会计师事务所（特殊普通合伙）\n中国注册会计师：莫威威"


def test_page_metadata_uses_all_band_votes_and_title_evidence() -> None:
    extractions = [
        VisualBandExtraction(
            company_name="江苏联通材料股份有限公司",
            statement_title="江苏联瑞新材料股份有限公司资产负债表",
        ),
        VisualBandExtraction(
            company_name="江苏联瑞新材料股份有限公司",
            statement_title="资产负债表",
        ),
        VisualBandExtraction(
            company_name="江苏联瑞新材料股份有限公司",
            statement_title="资产负债表",
        ),
    ]

    company_name, statement_title, statement_date, unit = VisualFusionService._page_metadata(
        extractions
    )

    assert company_name == "江苏联瑞新材料股份有限公司"
    assert statement_title == "资产负债表"
    assert statement_date is None
    assert unit is None


def test_page_metadata_uses_statement_title_to_break_company_tie() -> None:
    extractions = [
        VisualBandExtraction(
            company_name="江苏联通材料股份有限公司",
            statement_title="江苏联瑞新材料股份有限公司资产负债表",
        ),
        VisualBandExtraction(
            company_name="江苏联瑞新材料股份有限公司",
            statement_title="资产负债表",
        ),
    ]

    company_name, *_ = VisualFusionService._page_metadata(extractions)

    assert company_name == "江苏联瑞新材料股份有限公司"
