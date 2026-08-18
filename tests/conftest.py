from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from app.config import Settings
from app.lifespan import Runtime
from app.models import (
    BackendState,
    BackendStatus,
    ContentParseOptions,
    DocumentParseResult,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    ParsePipeline,
    ParseUsage,
    PipelineWarning,
    QualitySummary,
    RouteSummary,
    ServiceError,
    StoredSource,
)
from app.repositories import JobRepository
from app.services.cleanup_service import CleanupService
from app.services.ir_service import DocumentIRService
from app.services.job_service import JobService
from app.services.source_service import SourceService
from app.services.storage_service import StorageService
from app.utils.page_range import parse_page_range


class FakeParserService:
    """Deterministic parser used to exercise real HTTP, queue, SQLite, and storage."""

    def __init__(self) -> None:
        self.initialized = False
        self.last_options: ContentParseOptions | None = None

    async def initialize(self) -> None:
        self.initialized = True

    async def reload(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.initialized = False

    async def probe_backends(self) -> list[BackendStatus]:
        return [
            BackendStatus(name="docling-standard", state=BackendState.READY, detail="test adapter"),
            BackendStatus(
                name="glm-ocr-remote",
                state=BackendState.DISABLED,
                enabled=False,
                detail="disabled in tests",
            ),
            BackendStatus(
                name="ollama-vlm",
                state=BackendState.DISABLED,
                enabled=False,
                detail="disabled in tests",
            ),
        ]

    async def parse(
        self,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        document_id: str,
        progress_callback=None,
        cancel_event=None,
    ) -> DocumentParseResult:
        self.last_options = options
        if source.filename.startswith("fail"):
            raise ServiceError("mock_parse_failed", "Deterministic parser failure", status_code=502)
        if source.filename.startswith("slow"):
            for _ in range(100):
                if cancel_event is not None and cancel_event.is_set():
                    raise ServiceError("job_cancelled", "Job was cancelled", status_code=409)
                await asyncio.sleep(0.01)

        selected = parse_page_range(options.page_range, source.page_count)
        pages: list[PageParseResult] = []
        if progress_callback is not None:
            result = progress_callback(0, len(selected), "document.started")
            if inspect.isawaitable(result):
                await result
        for index, page_number in enumerate(selected, 1):
            pages.append(
                PageParseResult(
                    page_number=page_number,
                    backend="docling-standard",
                    content=f"# Page {page_number}\n\nValue: 12,345.67",
                    plain_text=f"Page {page_number}\n\nValue: 12,345.67",
                    duration_ms=1,
                    warnings=[
                        PipelineWarning(
                            code="fixture_info",
                            message="fixture diagnostic",
                            severity="info",
                            page_number=page_number,
                        )
                    ],
                    diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
                )
            )
            if progress_callback is not None:
                result = progress_callback(index, len(selected), "page.completed")
                if inspect.isawaitable(result):
                    await result
        markdown = "\n\n\f\n\n".join(page.content or "" for page in pages)
        plain_text = "\n\n\f\n\n".join(page.plain_text or "" for page in pages)
        parsed = DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=len(pages),
            markdown=markdown,
            plain_text=plain_text,
            pages=pages,
            pipeline=ParsePipeline(
                profile=options.profile.value,
                primary="docling-standard",
            ),
            route_summary=RouteSummary(native_text_pages=len(pages), failed_pages=0),
            warnings=[
                PipelineWarning(
                    code="fixture_info",
                    message="fixture diagnostic",
                    severity="info",
                )
            ],
            quality_summary=QualitySummary(trusted_pages=len(pages)),
            usage=ParseUsage(input_bytes=source.size_bytes, duration_ms=2),
        )
        parsed.parse_result = DocumentIRService().build(parsed, source, {})
        if not options.include_renderings:
            parsed.parse_result.renderings.markdown = ""
            parsed.parse_result.renderings.plain_text = ""
            for page in parsed.parse_result.units:
                page.renderings.markdown = ""
                page.renderings.plain_text = ""
        return parsed


def make_test_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "static_dir": tmp_path / "missing-static",
        "glm_ocr_enabled": False,
        "vlm_enabled": False,
        "docling_model_download": False,
        "mcp_enabled": False,
        "max_upload_bytes": 1_000_000,
        "max_content_units": 20,
        "sync_max_bytes": 1_000_000,
        "sync_max_units": 10,
        "max_queued_jobs": 4,
        "parser_workers": 1,
        "sse_heartbeat_seconds": 0.05,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def fake_runtime(settings: Settings) -> Runtime:
    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = FakeParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    return Runtime(
        settings=settings,
        repository=repository,
        storage_service=storage,
        source_service=source,
        parser_service=parser,  # type: ignore[arg-type]
        job_service=jobs,
        cleanup_service=CleanupService(repository, storage),
    )


@pytest.fixture
def native_pdf() -> Path:
    return Path("tests/fixtures/native-report.pdf").resolve()
