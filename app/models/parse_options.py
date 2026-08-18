"""Stable public parsing options.

The rest of the application deliberately depends on these small models instead of
Docling's (frequently evolving) option objects.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ParseProfile(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    ACCURATE = "accurate"


class VlmPolicy(StrEnum):
    """Internal routing decision derived from the quality profile."""

    OFF = "off"
    AUTO_VISUAL = "auto_visual"


LanguageCode = Annotated[str, Field(min_length=1, max_length=32)]


class ContentParseOptions(BaseModel):
    """User-facing options shared by synchronous and asynchronous content parsing."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    profile: ParseProfile = ParseProfile.BALANCED
    unit_range: str | None = Field(default=None, max_length=2_048)
    language: list[LanguageCode] = Field(
        default_factory=lambda: ["zh", "en"], min_length=1, max_length=16
    )
    include_renderings: bool = True
    description_language: Literal["zh-CN", "en", "auto"] = "zh-CN"
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @field_validator("unit_range")
    @classmethod
    def normalize_page_range(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        # Full semantic validation (including the document's upper bound) happens
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
        """Accurate enables visual fusion; other profiles never call Qwen."""

        if self.profile == ParseProfile.ACCURATE:
            return VlmPolicy.AUTO_VISUAL
        return VlmPolicy.OFF

    @property
    def page_range(self) -> str | None:
        """Internal adapter for the existing page-oriented document pipeline."""

        return self.unit_range
