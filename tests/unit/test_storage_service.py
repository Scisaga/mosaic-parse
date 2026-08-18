from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParsePipeline,
    ParseWarning,
    RouteSummary,
    StoredSource,
)
from app.services.ir_service import DocumentIRService
from app.services.storage_service import LegacyEvidenceError, StorageService


async def test_write_result_persists_content_metadata_and_warning_log(tmp_path: Path) -> None:
    storage = StorageService(SimpleNamespace(data_dir=tmp_path))
    warning = ParseWarning(code="low_text_content", message="too short", page_number=1)
    result = DocumentParseResult(
        document_id="job_warning",
        filename="report.pdf",
        mime_type="application/pdf",
        page_count=1,
        processed_pages=1,
        markdown="# Result",
        plain_text="Result",
        pages=[
            PageParseResult(
                page_number=1,
                status=PageStatus.WARNING,
                backend="docling-standard",
                content="secret document body",
                warnings=[warning],
            )
        ],
        pipeline=ParsePipeline(profile="balanced", primary="docling-standard"),
        route_summary=RouteSummary(failed_pages=0),
        warnings=[warning],
    )
    source_path = tmp_path / "report.pdf"
    source_path.write_bytes(b"fixture")
    result.evidence_ir = DocumentIRService().build(
        result,
        StoredSource(
            path=source_path,
            filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=source_path.stat().st_size,
            page_count=1,
        ),
        {},
    )

    paths = await storage.write_result("job_warning", result)

    assert paths.markdown.read_text() == "# Result"
    assert paths.text.read_text() == "Result"
    assert paths.ir.is_file()
    assert json.loads(paths.ir.read_text())["object"] == "content.evidence"
    payload = json.loads(paths.warnings.read_text())
    assert payload["document_warnings"][0]["code"] == "low_text_content"
    assert payload["units"][0]["unit_index"] == 1
    assert "secret document body" not in paths.warnings.read_text()


async def test_legacy_result_is_retained_but_not_silently_converted(tmp_path: Path) -> None:
    storage = StorageService(SimpleNamespace(data_dir=tmp_path))
    await storage.create_job_layout("job_legacy")
    (storage.output_dir("job_legacy") / "result.json").write_text(
        '{"schema_version":"document-evidence/1.0","object":"document.evidence"}',
        encoding="utf-8",
    )
    with pytest.raises(LegacyEvidenceError):
        await storage.read_evidence("job_legacy")
