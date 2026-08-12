from app.models import (
    DocumentParseResult,
    PageParseResult,
    ParsePipeline,
    ParseUsage,
)
from app.services.export_service import ExportService
from app.services.quality_service import QualityService


def test_markdown_to_plain_text_removes_common_markup() -> None:
    markdown = "# 标题\n\n- [链接](https://example.com)\n\n**重点**"
    assert ExportService.markdown_to_text(markdown) == "标题\n\n链接\n\n重点"


def test_page_breaks_are_explicit_and_deterministic() -> None:
    pages = [
        PageParseResult(page_number=1, content="# One"),
        PageParseResult(page_number=2, content="# Two"),
    ]
    markdown, text = ExportService().join_pages(pages, preserve_page_breaks=True)
    assert "<!-- page: 1 -->" in markdown
    assert "\f" in markdown
    assert "--- Page 2 ---" in text


def test_quality_flags_empty_output_without_inventing_metrics() -> None:
    result = DocumentParseResult(
        document_id="docparse_test",
        filename="report.pdf",
        mime_type="application/pdf",
        page_count=1,
        processed_pages=1,
        pages=[PageParseResult(page_number=1, content="")],
        pipeline=ParsePipeline(mode="auto", profile="balanced", primary="docling-standard"),
        usage=ParseUsage(input_bytes=100, duration_ms=1),
    )
    assessment = QualityService().assess(result)
    assert not assessment.acceptable
    assert result.warnings[0].code == "low_text_content"
    assert result.route_summary.native_text_pages is None
    assert result.route_summary.ocr_regions is None


def test_quality_detects_long_broken_tounicode_latin_run_conservatively() -> None:
    quality = QualityService()

    assert quality.has_suspicious_unicode_mojibake("犚 犲 犿 犪 狀 狌 犳 犪 犮 狋 狌 狉 犻 狀 犵")
    assert not quality.has_suspicious_unicode_mojibake("附录犃，符合 GB/T 40727")
    assert not quality.has_suspicious_unicode_mojibake("普通中文段落和正常 English text")


def test_quality_ignores_repeated_low_information_link_placeholders() -> None:
    quality = QualityService()
    page = PageParseResult(
        page_number=1,
        content="正文内容足够长。" + "来源链接。" * 6 + "参 考 链 接。" * 6 + "链接。" * 6,
    )

    assert not any(warning.code == "repeated_text" for warning in quality.inspect_page(page))

    page.content = "真正重复且有信息的内容。" * 5
    assert any(warning.code == "repeated_text" for warning in quality.inspect_page(page))
