from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import (
    AssetKind,
    AssetLocation,
    AssetRole,
    ContentAsset,
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParsePipeline,
    PipelineWarning,
    RouteSummary,
    StoredSource,
)
from app.services.ir_service import DocumentIRService
from app.services.storage_service import AssetIndexError, StorageService


async def test_write_result_persists_content_metadata_and_warning_log(tmp_path: Path) -> None:
    storage = StorageService(SimpleNamespace(data_dir=tmp_path))
    warning = PipelineWarning(code="low_text_content", message="too short", page_number=1)
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
    result.parse_result = DocumentIRService().build(
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
    result.parse_result.assets = [
        ContentAsset(
            asset_id="asset_fixture",
            kind=AssetKind.IMAGE,
            role=AssetRole.EMBEDDED_IMAGE,
            mime_type="image/png",
            sha256="0" * 64,
            size_bytes=1,
            filename="figure.png",
            locations=[
                AssetLocation(
                    unit_id="p1",
                    page_number=1,
                    bbox={"left": 0.1, "top": 0.2, "right": 0.3, "bottom": 0.4},
                )
            ],
            download_url="/v1/content/jobs/job_warning/assets/asset_fixture",
        )
    ]

    paths = await storage.write_result("job_warning", result)

    assert paths.result.read_text() == "# Result"
    assert paths.result.name == "result.md"
    assert not (storage.output_dir("job_warning") / "result.json").exists()
    index = json.loads((storage.output_dir("job_warning") / "asset-index.json").read_text())
    assert index["schema"] == "mosaic-asset-index/2.0"
    assert index["assets"][0]["asset_id"] == "asset_fixture"
    assert "locations" not in index["assets"][0]
    assert "units" not in index
    assert "tables" not in index
    payload = json.loads(paths.warnings.read_text())
    assert payload["document_warnings"][0]["code"] == "low_text_content"
    assert payload["units"][0]["unit_index"] == 1
    assert "secret document body" not in paths.warnings.read_text()


async def test_invalid_asset_index_is_rejected(tmp_path: Path) -> None:
    storage = StorageService(SimpleNamespace(data_dir=tmp_path))
    await storage.create_job_layout("job_invalid")
    (storage.output_dir("job_invalid") / "asset-index.json").write_text(
        '{"schema":"retired/1.0","assets":[]}', encoding="utf-8"
    )
    with pytest.raises(AssetIndexError):
        await storage.read_asset_index("job_invalid")
