from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.models import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParsePipeline,
    ParseWarning,
    RouteSummary,
)
from app.services.storage_service import StorageService


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
        pipeline=ParsePipeline(mode="auto", profile="balanced", primary="docling-standard"),
        route_summary=RouteSummary(failed_pages=0),
        warnings=[warning],
    )

    paths = await storage.write_result("job_warning", result)

    assert paths.markdown.read_text() == "# Result"
    assert paths.text.read_text() == "Result"
    assert paths.metadata.is_file()
    payload = json.loads(paths.warnings.read_text())
    assert payload["document_warnings"][0]["code"] == "low_text_content"
    assert payload["pages"][0]["page_number"] == 1
    assert "secret document body" not in paths.warnings.read_text()
