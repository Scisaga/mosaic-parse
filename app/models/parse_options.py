"""Stable public parsing options.

The rest of the application deliberately depends on these small models instead of
Docling's (frequently evolving) option objects.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParseProfile(StrEnum):
    # Adapter-only compatibility setting for persisted pre-0.4 jobs. New
    # HTTP/MCP requests never expose a quality profile.
    FAST = "fast"
    BALANCED = "balanced"
    ACCURATE = "accurate"


class ScanPolicy(StrEnum):
    """Public policy for scanned PDF pages."""

    AUTO = "auto"
    SKIP = "skip"


class VlmPolicy(StrEnum):
    """Internal routing decision derived from the scan policy."""

    OFF = "off"
    AUTO_VISUAL = "auto_visual"


LanguageCode = Annotated[str, Field(min_length=1, max_length=32)]


class ContentParseOptions(BaseModel):
    """User-facing options shared by synchronous and asynchronous content parsing."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    # Retained only to decode old database records and tune internal adapters.
    # It is deliberately absent from public request/response contracts.
    profile: ParseProfile = Field(default=ParseProfile.BALANCED, exclude=True)
    scan_policy: ScanPolicy = ScanPolicy.AUTO
    unit_range: str | None = Field(default=None, max_length=2_048)
    language: list[LanguageCode] = Field(
        default_factory=lambda: ["zh", "en"], min_length=1, max_length=16
    )
    # Embedded PDF/Office images are always preserved as assets. Describing
    # every image can be expensive, so model-generated descriptions are opt-in.
    # Standalone image inputs are still analysed because the image is the source.
    describe_images: bool = False
    description_language: Literal["zh-CN", "en", "auto"] = "zh-CN"
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_profile(cls, value: object) -> object:
        """Map persisted profile-only options onto the scan-policy contract."""

        if not isinstance(value, dict) or "scan_policy" in value or "profile" not in value:
            return value
        normalized = dict(value)
        raw_profile = normalized.get("profile")
        if isinstance(raw_profile, ParseProfile):
            raw_profile = raw_profile.value
        normalized["scan_policy"] = (
            ScanPolicy.AUTO.value
            if raw_profile == ParseProfile.ACCURATE.value
            else ScanPolicy.SKIP.value
        )
        return normalized

    @field_validator("unit_range")
    @classmethod
    def normalize_page_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        # Full range validation (including the document's upper bound) happens
        # after the source has been inspected.
        from app.utils.page_range import parse_page_range

        parse_page_range(value)
        return value

    @field_validator("language", mode="before")
    @classmethod
    def parse_languages(cls, value: object) -> object:
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("language")
    @classmethod
    def unique_languages(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for language in value:
            normalized = language.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        if not result:
            raise ValueError("at least one OCR language is required")
        return result

    @property
    def resolved_vlm_policy(self) -> VlmPolicy:
        """Automatic scan processing enables selective visual fusion."""

        if self.scan_policy == ScanPolicy.AUTO:
            return VlmPolicy.AUTO_VISUAL
        return VlmPolicy.OFF

    @property
    def visual_profile(self) -> ParseProfile:
        """Use the measured high-resolution adapter path for automatic scans."""

        if self.scan_policy == ScanPolicy.AUTO:
            return ParseProfile.ACCURATE
        return ParseProfile.BALANCED

    @property
    def page_range(self) -> str | None:
        """Internal adapter for the existing page-oriented document pipeline."""

        return self.unit_range
