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
    ContentParseOptions,
    ContentParseResult,
    JobError,
    JobProgress,
    JobRecord,
    JobStatus,
)

ParseResponse = ContentParseResult


class JobResponse(BaseModel):
    """Safe task representation returned by job endpoints."""

    model_config = ConfigDict(extra="forbid")

    id: str
    object: Literal["content.parse.job"] = "content.parse.job"
    status: JobStatus
    filename: str
    mime_type: str
    unit_count: int = Field(ge=1)
    progress: JobProgress
    options: ContentParseOptions
    error: JobError | None = None
    attempt: int = Field(ge=1)
    parent_job_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status_url: str
    events_url: str
    result_url: str
    assets_url: str
    bundle_url: str

    @classmethod
    def from_record(
        cls,
        record: JobRecord,
    ) -> JobResponse:
        base = f"/v1/content/jobs/{record.id}"
        return cls(
            id=record.id,
            status=record.status,
            filename=record.filename,
            mime_type=record.mime_type,
            unit_count=record.page_count,
            progress=record.progress,
            options=record.options,
            error=record.error,
            attempt=record.attempt,
            parent_job_id=record.parent_job_id,
            created_at=record.created_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            status_url=base,
            events_url=f"{base}/events",
            result_url=f"{base}/result",
            assets_url=f"{base}/assets",
            bundle_url=f"{base}/bundle",
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
