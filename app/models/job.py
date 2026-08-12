"""Persistent job models and the v0.1 state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.parse_options import DocumentParseOptions


def utc_now() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.PARTIAL,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
}

ALLOWED_JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.RUNNING: {
        JobStatus.COMPLETED,
        JobStatus.PARTIAL,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.COMPLETED: set(),
    JobStatus.PARTIAL: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    unit: str = "page"

    @property
    def percent(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(min(100.0, self.current * 100.0 / self.total), 1)


class JobError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, object] | None = None


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    object: str = "document.parse.job"
    status: JobStatus = JobStatus.QUEUED
    filename: str
    mime_type: str
    input_bytes: int = Field(ge=0)
    page_count: int = Field(ge=1)
    source_path: str
    source_url: str | None = None
    options: DocumentParseOptions = Field(default_factory=DocumentParseOptions)
    progress: JobProgress = Field(default_factory=JobProgress)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    result_markdown_path: str | None = None
    result_text_path: str | None = None
    metadata_path: str | None = None
    error: JobError | None = None
    attempt: int = Field(default=1, ge=1)
    parent_job_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES

    def ensure_transition(self, target: JobStatus) -> None:
        if target == self.status:
            return
        if target not in ALLOWED_JOB_TRANSITIONS[self.status]:
            raise ValueError(f"invalid job transition: {self.status.value} -> {target.value}")


class JobEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    event: str
    job_id: str
    data: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
