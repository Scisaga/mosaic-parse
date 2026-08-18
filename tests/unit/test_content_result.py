from pathlib import Path

from app.models import (
    DocumentParseResult,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    ParsePipeline,
    QualitySummary,
    RouteSummary,
    StoredSource,
)
from app.services.evidence_service import NativeBlock, PageEvidence
from app.services.ir_service import DocumentIRService
from app.services.table_service import TableCellFragment, TableFragment, render_gfm_rows
from app.services.visual_fusion_service import VisualPageIR, VisualSignatureExtraction


def test_content_result_preserves_spans_provenance_and_non_body_visual_material(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "result.pdf"
    source_path.write_bytes(b"stable fixture bytes")
    source = StoredSource(
        path=source_path,
        filename=source_path.name,
        mime_type="application/pdf",
        size_bytes=source_path.stat().st_size,
        page_count=1,
    )
    rows = [["项目", "金额", ""], ["收入", "100", "90"]]
    fragment = TableFragment(
        fragment_id="p1_t1",
        page_number=1,
        ordinal=1,
        normalized_bbox=(0.1, 0.4, 0.9, 0.8),
        num_rows=2,
        num_cols=3,
        rows=rows,
        column_boundaries=[0.1, 0.4, 0.65, 0.9],
        markdown=render_gfm_rows(rows),
        rendered=render_gfm_rows(rows),
        has_column_header=True,
        source_kind="qwen_visual_fusion",
        cells=[
            TableCellFragment(0, 0, 1, 1, "项目", (0.1, 0.4, 0.4, 0.5), True),
            TableCellFragment(0, 1, 1, 2, "金额", (0.4, 0.4, 0.9, 0.5), True),
            TableCellFragment(1, 0, 1, 1, "收入", (0.1, 0.5, 0.4, 0.6), False, True),
            TableCellFragment(1, 1, 1, 1, "100", (0.4, 0.5, 0.65, 0.6)),
            TableCellFragment(1, 2, 1, 1, "90", (0.65, 0.5, 0.9, 0.6)),
        ],
        cell_evidence=[
            [
                {"qwen": "项目", "glm": "项目", "final": "项目"},
                {"qwen": "金额", "glm": "金额", "final": "金额"},
                {},
            ],
            [
                {"qwen": "收入", "glm": "收入", "final": "收入"},
                {"qwen": "100", "glm": "100", "final": "100"},
                {"qwen": "90", "docling": "90", "final": "90"},
            ],
        ],
    )
    result = DocumentParseResult(
        document_id="doc_ir_test",
        filename=source.filename,
        mime_type=source.mime_type,
        page_count=1,
        processed_pages=1,
        markdown="# 报告\n\n" + fragment.markdown,
        plain_text="报告\n项目 金额\n收入 100 90",
        pages=[
            PageParseResult(
                page_number=1,
                content="# 报告\n\n" + fragment.markdown + "\n\n<!-- image -->",
                plain_text="报告\n项目 金额\n收入 100 90",
                diagnostics=PageDiagnostics(source_kind=PageSourceKind.MIXED),
            )
        ],
        pipeline=ParsePipeline(
            profile="accurate",
            primary="visual-fusion",
            ocr="glm-sdk-remote",
            vlm="ollama-vlm",
        ),
        route_summary=RouteSummary(vlm_pages=1, failed_pages=0),
        quality_summary=QualitySummary(trusted_pages=1, visual_pages=1, qwen_calls=1),
    )
    result._table_fragments = [fragment]
    result._visual_page_irs[1] = VisualPageIR(
        page_number=1,
        rotation_degrees=0,
        signature=VisualSignatureExtraction(
            printed_lines=["法定代表人 张三"],
            duplicate_printed_lines=[],
            visual_only_names=["李四"],
            seal_or_handwriting_ocr_line_ids=[],
            has_seal=True,
            has_handwriting=True,
        ),
    )
    page_evidence = PageEvidence(
        page_number=1,
        source_kind=PageSourceKind.MIXED,
        native_blocks=[
            NativeBlock("报告", (10, 10, 90, 20), 16, True),
            NativeBlock("收入 100 90", (10, 45, 90, 60), 10, False),
        ],
        page_width=100,
        page_height=100,
    )

    parse_result = DocumentIRService().build(result, source, {1: page_evidence})

    assert parse_result.object == "content.parse_result"
    assert parse_result.schema_version == "content-parse-result/1.0"
    assert [block.text for block in parse_result.units[0].blocks] == ["报告", "李四"]
    assert {region.region_type.value for region in parse_result.units[0].regions} >= {
        "heading",
        "table",
        "seal",
        "handwriting",
    }
    merged_header = next(cell for cell in parse_result.tables[0].cells if cell.text == "金额")
    assert merged_header.column_span == 2
    assert merged_header.quality.value == "confirmed"
    amount = next(cell for cell in parse_result.tables[0].cells if cell.text == "100")
    assert amount.provenance.selected_source.value in {"glm", "qwen"}
    assert set(amount.provenance.supporting_sources) == {"glm", "qwen"}
    handwriting = next(block for block in parse_result.units[0].blocks if block.text == "李四")
    assert handwriting.provenance.selected_source.value == "qwen"
    assert handwriting.provenance.reason_codes == ["visual_only_handwriting"]
