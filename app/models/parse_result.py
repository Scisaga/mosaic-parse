"""Parser-independent document result models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


def utc_now() -> datetime:
    return datetime.now(UTC)


class WarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class PageStatus(StrEnum):
    COMPLETED = "completed"
    WARNING = "warning"
    FAILED = "failed"


class ParseWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4_096)
    severity: WarningSeverity = WarningSeverity.WARNING
    page_number: int | None = Field(default=None, ge=1)
    backend: str | None = None
    details: dict[str, object] | None = None


class PageParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    status: PageStatus = PageStatus.COMPLETED
    backend: str | None = None
    content: str | None = None
    plain_text: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    warnings: list[ParseWarning] = Field(default_factory=list)


class ParsePipeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    profile: str
    primary: str
    ocr: str | None = None
    vlm: str | None = None


class RouteSummary(BaseModel):
    """Only values observed from an adapter may be populated.

    Docling does not consistently expose page-level routing details, hence most
    counters are nullable rather than guessed.
    """

    model_config = ConfigDict(extra="forbid")

    native_text_pages: int | None = Field(default=None, ge=0)
    pages_with_ocr: int | None = Field(default=None, ge=0)
    ocr_regions: int | None = Field(default=None, ge=0)
    vlm_pages: int | None = Field(default=None, ge=0)
    failed_pages: int = Field(default=0, ge=0)


class ParseUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_bytes: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class DocumentParseResult(BaseModel):
    """Stable result; no Docling object is allowed across this boundary."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    filename: str
    mime_type: str
    page_count: int = Field(ge=1)
    processed_pages: int = Field(ge=0)
    markdown: str = ""
    plain_text: str = ""
    pages: list[PageParseResult] = Field(default_factory=list)
    pipeline: ParsePipeline
    route_summary: RouteSummary = Field(default_factory=RouteSummary)
    warnings: list[ParseWarning] = Field(default_factory=list)
    usage: ParseUsage = Field(default_factory=ParseUsage)
    created_at: datetime = Field(default_factory=utc_now)

    # Adapter-private, request-scoped enrichment hints.  These never cross the
    # stable API/storage boundary and deliberately stay out of the JSON schema.
    _picture_candidates: list[object] = PrivateAttr(default_factory=list)
    _vlm_page_numbers: set[int] = PrivateAttr(default_factory=set)

    def content_for(self, output_format: str) -> str:
        return self.plain_text if str(output_format) == "text" else self.markdown
