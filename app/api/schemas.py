"""Stable public HTTP response models.

These models intentionally hide storage paths and other worker implementation
details from OpenAPI and callers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    BackendStatus,
    DocumentParseOptions,
    DocumentParseResult,
    JobError,
    JobProgress,
    JobRecord,
    JobStatus,
    OutputFormat,
    PageParseResult,
    ParsePipeline,
    ParseUsage,
    ParseWarning,
    RouteSummary,
)


class ParseResponse(BaseModel):
    """Synchronous document conversion response."""

    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["document.parse"] = "document.parse"
    status: Literal["completed", "partial"] = "completed"
    filename: str
    mime_type: str
    page_count: int = Field(ge=1)
    processed_pages: int = Field(ge=0)
    output_format: OutputFormat
    content: str
    pipeline: ParsePipeline
    route_summary: RouteSummary
    warnings: list[ParseWarning] = Field(default_factory=list)
    usage: ParseUsage
    pages: list[PageParseResult] | None = None
    created_at: datetime

    @classmethod
    def from_result(
        cls,
        result: DocumentParseResult,
        options: DocumentParseOptions,
    ) -> ParseResponse:
        status: Literal["completed", "partial"] = (
            "partial" if result.route_summary.failed_pages else "completed"
        )
        return cls(
            id=result.document_id,
            status=status,
            filename=result.filename,
            mime_type=result.mime_type,
            page_count=result.page_count,
            processed_pages=result.processed_pages,
            output_format=options.output_format,
            content=result.content_for(options.output_format.value),
            pipeline=result.pipeline,
            route_summary=result.route_summary,
            warnings=result.warnings if options.include_diagnostics else [],
            usage=result.usage,
            pages=result.pages if options.include_pages else None,
            created_at=result.created_at,
        )


class JobResponse(BaseModel):
    """Safe task representation returned by job endpoints."""

    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["document.parse.job"] = "document.parse.job"
    status: JobStatus
    filename: str
    mime_type: str
    page_count: int = Field(ge=1)
    processed_pages: int | None = Field(default=None, ge=0)
    progress: JobProgress
    options: DocumentParseOptions
    error: JobError | None = None
    attempt: int = Field(ge=1)
    parent_job_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    pages: list[PageParseResult] | None = None
    pipeline: ParsePipeline | None = None
    route_summary: RouteSummary | None = None
    warnings: list[ParseWarning] | None = None
    usage: ParseUsage | None = None
    status_url: str
    events_url: str
    result_url: str

    @classmethod
    def from_record(
        cls,
        record: JobRecord,
        result: DocumentParseResult | None = None,
    ) -> JobResponse:
        base = f"/v1/documents/jobs/{record.id}"
        return cls(
            id=record.id,
            status=record.status,
            filename=record.filename,
            mime_type=record.mime_type,
            page_count=record.page_count,
            processed_pages=result.processed_pages if result else None,
            progress=record.progress,
            options=record.options,
            error=record.error,
            attempt=record.attempt,
            parent_job_id=record.parent_job_id,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            pages=result.pages if result and record.options.include_pages else None,
            pipeline=result.pipeline if result else None,
            route_summary=result.route_summary if result else None,
            warnings=(
                result.warnings
                if result and record.options.include_diagnostics
                else None
            ),
            usage=(
                result.usage
                if result and record.options.include_diagnostics
                else None
            ),
            status_url=base,
            events_url=f"{base}/events",
            result_url=f"{base}/result",
        )


class QueueStatus(BaseModel):
    active: int = Field(ge=0)
    capacity: int = Field(ge=1)


class BackendsResponse(BaseModel):
    backends: list[BackendStatus]
    queue: QueueStatus


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    uptime_seconds: float = Field(ge=0)
    queue: QueueStatus
    config: dict[str, object]
    backends: list[BackendStatus]


class ReadyCheck(BaseModel):
    writable_data_dir: bool
    writable_database: bool
    docling: bool


class ReadyResponse(BaseModel):
    ready: bool
    status: Literal["ready", "not_ready"]
    checks: ReadyCheck
    backends: list[BackendStatus]


class DeleteJobResponse(BaseModel):
    id: str
    status: Literal["cancelled", "deleted"]


class ReloadResponse(BaseModel):
    status: Literal["reloaded"] = "reloaded"
    backends: list[BackendStatus]


class CleanupResponse(BaseModel):
    status: Literal["completed"] = "completed"
    deleted_jobs: int = Field(ge=0)
