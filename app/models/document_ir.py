"""Stable, domain-neutral multimodal content evidence contract."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ElementQuality(StrEnum):
    COMPLETE = "complete"
    CONFIRMED = "confirmed"
    SELECTED = "selected"
    CONFLICTED = "conflicted"
    MISSING = "missing"
    TRUNCATED = "truncated"


class EvidenceSourceKind(StrEnum):
    NATIVE = "native"
    DOCLING = "docling"
    GLM = "glm"
    QWEN = "qwen"
    FFMPEG = "ffmpeg"
    OOXML = "ooxml"


class SourceKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    IMAGE = "image"
    VIDEO = "video"


class UnitType(StrEnum):
    PAGE = "page"
    SLIDE = "slide"
    DOCUMENT_BODY = "document_body"
    IMAGE = "image"
    VIDEO = "video"


class RegionType(StrEnum):
    TEXT = "text"
    HEADING = "heading"
    TABLE = "table"
    IMAGE = "image"
    SIGNATURE = "signature"
    SEAL = "seal"
    HANDWRITING = "handwriting"
    FORMULA = "formula"
    UNKNOWN = "unknown"


class AssetKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class AssetRole(StrEnum):
    SOURCE = "source"
    EMBEDDED_IMAGE = "embedded_image"
    PAGE_CROP = "page_crop"
    PREVIEW = "preview"
    KEYFRAME = "keyframe"


class AssetStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"


class VisualClassification(StrEnum):
    DOCUMENT = "document"
    VISUAL = "visual"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class NormalizedBBox(BaseModel):
    """Top-left-origin box normalized to the inclusive range 0..1."""

    model_config = ConfigDict(extra="forbid")

    left: float = Field(ge=0, le=1)
    top: float = Field(ge=0, le=1)
    right: float = Field(ge=0, le=1)
    bottom: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self) -> NormalizedBBox:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("bbox coordinates must be ordered")
        return self


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: EvidenceSourceKind
    backend: str | None = None


class ElementEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_source: EvidenceSourceKind | None = None
    supporting_sources: list[EvidenceSourceKind] = Field(default_factory=list)
    sources: list[EvidenceSource] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class TextBlockIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    unit_id: str
    region_id: str
    block_type: RegionType = RegionType.TEXT
    bbox: NormalizedBBox | None = None
    reading_order: int = Field(ge=0)
    text: str
    quality: ElementQuality = ElementQuality.COMPLETE
    evidence: ElementEvidence = Field(default_factory=ElementEvidence)


class RegionIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region_id: str
    unit_id: str
    region_type: RegionType
    bbox: NormalizedBBox | None = None
    reading_order: int = Field(ge=0)
    block_ids: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    quality: ElementQuality = ElementQuality.COMPLETE


class TableCellIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cell_id: str
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    row_span: int = Field(default=1, ge=1)
    column_span: int = Field(default=1, ge=1)
    bbox: NormalizedBBox | None = None
    text: str
    is_column_header: bool = False
    is_row_header: bool = False
    quality: ElementQuality = ElementQuality.COMPLETE
    evidence: ElementEvidence = Field(default_factory=ElementEvidence)


class TableIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str
    unit_id: str
    region_id: str
    source_units: list[int] = Field(min_length=1)
    bbox: NormalizedBBox | None = None
    caption: str | None = None
    unit_text: str | None = None
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=0)
    header_rows: list[int] = Field(default_factory=list)
    cells: list[TableCellIR] = Field(default_factory=list)
    logical_table_id: str | None = None
    quality: ElementQuality = ElementQuality.COMPLETE
    reason_codes: list[str] = Field(default_factory=list)


class LogicalTableIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical_table_id: str
    fragment_table_ids: list[str] = Field(min_length=1)
    source_units: list[int] = Field(min_length=1)
    header_policy: Literal["first_fragment"] = "first_fragment"


class ContentRenderings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = ""
    plain_text: str = ""


class UnitDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["native", "scanned", "mixed", "sparse", "visual", "office", "video"]
    quality_verdict: Literal["trusted", "degraded", "untrusted"]
    selected_strategy: Literal[
        "docling",
        "native_repair",
        "qwen_visual_fusion",
        "office_native",
        "visual_description",
        "video_keyframes",
    ]
    native_text_characters: int | None = Field(default=None, ge=0)
    visual_ink_ratio: float | None = Field(default=None, ge=0, le=1)
    image_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    detected_rotation_degrees: Literal[0, 90, 180, 270] | None = None
    warning_codes: list[str] = Field(default_factory=list)
    qwen_calls: int | None = Field(default=None, ge=0)
    qwen_duration_ms: int | None = Field(default=None, ge=0)
    unresolved_conflicts: int | None = Field(default=None, ge=0)
    truncated_calls: int | None = Field(default=None, ge=0)


class ContentUnitIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str
    unit_type: UnitType
    index: int = Field(ge=1)
    status: Literal["completed", "warning", "failed"]
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    rotation_degrees: Literal[0, 90, 180, 270] | None = None
    regions: list[RegionIR] = Field(default_factory=list)
    blocks: list[TextBlockIR] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    renderings: ContentRenderings = Field(default_factory=ContentRenderings)
    diagnostics: UnitDiagnostics
    duration_ms: int = Field(default=0, ge=0)


class ContentSourceIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str
    mime_type: str
    kind: SourceKind
    size_bytes: int = Field(ge=0)
    unit_count: int = Field(ge=1)
    page_count: int | None = Field(default=None, ge=1)
    slide_count: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)


class AssetLocationIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    slide_number: int | None = Field(default=None, ge=1)
    bbox: NormalizedBBox | None = None
    timestamp_ms: int | None = Field(default=None, ge=0)
    relationship_id: str | None = None
    placement_id: str | None = None


class VisualAnalysisIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: VisualClassification
    summary: str
    detailed_description: str = ""
    visible_text: str = ""
    scene: str | None = None
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    language: Literal["zh-CN", "en", "auto"] = "zh-CN"
    model: str
    uncertainties: list[str] = Field(default_factory=list)


class AssetIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    kind: AssetKind
    role: AssetRole
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    filename: str
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_ms: int | None = Field(default=None, ge=0)
    parent_asset_id: str | None = None
    locations: list[AssetLocationIR] = Field(default_factory=list)
    visual_analysis: VisualAnalysisIR | None = None
    status: AssetStatus = AssetStatus.READY
    warning_codes: list[str] = Field(default_factory=list)
    download_url: str


class VideoSceneIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    keyframe_asset_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self) -> VideoSceneIR:
        if self.end_ms < self.start_ms:
            raise ValueError("video scene end must not precede start")
        return self


class VideoKeyframeIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    timestamp_ms: int = Field(ge=0)
    scene_id: str | None = None
    visual_analysis: VisualAnalysisIR


class VideoAnalysisIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_ms: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    codec: str | None = None
    frame_rate: float | None = Field(default=None, gt=0)
    visual_only: Literal[True] = True
    summary: str
    scenes: list[VideoSceneIR] = Field(default_factory=list)
    keyframes: list[VideoKeyframeIR] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_and_bounded(self) -> VideoAnalysisIR:
        timestamps = [item.timestamp_ms for item in self.keyframes]
        if timestamps != sorted(timestamps):
            raise ValueError("video keyframe timestamps must be monotonic")
        if any(timestamp > self.duration_ms for timestamp in timestamps):
            raise ValueError("video keyframe timestamp exceeds measured duration")
        if any(scene.end_ms > self.duration_ms for scene in self.scenes):
            raise ValueError("video scene exceeds measured duration")
        return self


class ContentQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trusted_units: int = Field(default=0, ge=0)
    degraded_units: int = Field(default=0, ge=0)
    untrusted_units: int = Field(default=0, ge=0)
    repaired_units: int = Field(default=0, ge=0)
    visual_units: int = Field(default=0, ge=0)
    unresolved_visual_conflicts: int = Field(default=0, ge=0)


class ParseRuntimeIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["fast", "balanced", "accurate"]
    primary_backend: str
    ocr_backend: str | None = None
    visual_backend: str | None = None
    parser_version: str
    input_bytes: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    qwen_calls: int = Field(default=0, ge=0)
    ffmpeg_duration_ms: int | None = Field(default=None, ge=0)


class IRWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["info", "warning", "error"]
    unit_index: int | None = Field(default=None, ge=1)
    region_id: str | None = None
    asset_id: str | None = None
    count: int | None = Field(default=None, ge=0)


class ContentLinksIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: str
    events: str
    result: str
    assets: str
    bundle: str


class ContentEvidenceIR(BaseModel):
    """Primary parse product consumed by external retrieval systems."""

    model_config = ConfigDict(extra="forbid")

    object: Literal["content.evidence"] = "content.evidence"
    schema_version: Literal["content-evidence/1.0"] = "content-evidence/1.0"
    status: Literal["completed", "partial"] = "completed"
    source: ContentSourceIR
    units: list[ContentUnitIR]
    assets: list[AssetIR] = Field(default_factory=list)
    tables: list[TableIR] = Field(default_factory=list)
    logical_tables: list[LogicalTableIR] = Field(default_factory=list)
    visual_analysis: VisualAnalysisIR | None = None
    video_analysis: VideoAnalysisIR | None = None
    renderings: ContentRenderings = Field(default_factory=ContentRenderings)
    diagnostics: ContentQualitySummary = Field(default_factory=ContentQualitySummary)
    warnings: list[IRWarning] = Field(default_factory=list)
    runtime: ParseRuntimeIR
    links: ContentLinksIR
    created_at: datetime = Field(default_factory=utc_now)
