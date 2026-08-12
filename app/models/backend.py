"""Backend capability reporting models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BackendState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class BackendStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: BackendState
    enabled: bool = True
    detail: str | None = None
    model: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def ready(self) -> bool:
        return self.state == BackendState.READY
