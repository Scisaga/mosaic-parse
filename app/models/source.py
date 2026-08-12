"""Validated, locally materialized input source."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class StoredSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    path: Path
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    page_count: int = Field(ge=1)
    source_url: str | None = None
