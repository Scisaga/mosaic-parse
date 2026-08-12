"""Conservative, explainable output-quality diagnostics."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.models.parse_result import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParseWarning,
    WarningSeverity,
)
from app.utils.settings import setting


@dataclass(slots=True)
class QualityAssessment:
    acceptable: bool
    warnings: list[ParseWarning] = field(default_factory=list)
    fallback_pages: list[int] = field(default_factory=list)


class QualityService:
    # Some Chinese standards contain a broken ToUnicode map where Latin glyphs
    # decode as a run of CJK characters from the cattle/dog radical blocks,
    # e.g. "Remanufacturing" becomes "犚犲犿犪狀...". Requiring a long run keeps
    # ordinary Chinese words and short standard identifiers such as GB/T out.
    _MOJIBAKE_LATIN_RUN = re.compile(r"(?:[\u7280-\u72ff][ \t]*){6,}")
    _LOW_INFORMATION_REPEAT = re.compile(r"^(?:(?:来源|参考)链接|链接)(?:地址)?[：:]?$", re.IGNORECASE)

    def __init__(self, settings: object | None = None) -> None:
        self.min_page_characters = int(setting(settings, "quality_min_page_characters", 12))
        self.replacement_ratio = float(setting(settings, "quality_max_replacement_ratio", 0.02))
        self.repeat_threshold = int(setting(settings, "quality_repeat_threshold", 5))

    def inspect_page(self, page: PageParseResult) -> list[ParseWarning]:
        content = (page.content or "").strip()
        warnings: list[ParseWarning] = []
        if page.status == PageStatus.FAILED:
            return page.warnings
        if len(content) < self.min_page_characters:
            warnings.append(
                ParseWarning(
                    code="low_text_content",
                    message=f"page contains fewer than {self.min_page_characters} non-whitespace characters",
                    page_number=page.page_number,
                    backend=page.backend,
                )
            )
        if content:
            replacements = content.count("\ufffd")
            if replacements / len(content) > self.replacement_ratio:
                warnings.append(
                    ParseWarning(
                        code="high_replacement_character_ratio",
                        message="page contains an unusually high Unicode replacement-character ratio",
                        page_number=page.page_number,
                        backend=page.backend,
                    )
                )
            fragments = [
                fragment
                for part in re.split(r"[\n。！？.!?]+", content)
                if 4 <= len(fragment := part.strip()) <= 120
                and not self._LOW_INFORMATION_REPEAT.fullmatch(re.sub(r"[ \t]+", "", fragment))
            ]
            repeated = [(fragment, count) for fragment, count in Counter(fragments).items() if count >= self.repeat_threshold]
            if repeated:
                warnings.append(
                    ParseWarning(
                        code="repeated_text",
                        message="page contains an abnormally repeated short fragment",
                        page_number=page.page_number,
                        backend=page.backend,
                        details={"repeat_count": max(count for _, count in repeated)},
                    )
                )
        return warnings

    @classmethod
    def has_suspicious_unicode_mojibake(cls, content: str | None) -> bool:
        return bool(content and cls._MOJIBAKE_LATIN_RUN.search(content))

    @classmethod
    def suspicious_unicode_pages(cls, result: DocumentParseResult) -> list[int]:
        return [
            page.page_number
            for page in result.pages
            if page.status != PageStatus.FAILED and cls.has_suspicious_unicode_mojibake(page.content)
        ]

    def assess(self, result: DocumentParseResult) -> QualityAssessment:
        warnings: list[ParseWarning] = []
        fallback_pages: list[int] = []
        for page in result.pages:
            detected = self.inspect_page(page)
            existing_codes = {warning.code for warning in page.warnings}
            page.warnings.extend(warning for warning in detected if warning.code not in existing_codes)
            if page.warnings and page.status == PageStatus.COMPLETED:
                page.status = PageStatus.WARNING
            if page.status in {PageStatus.WARNING, PageStatus.FAILED}:
                fallback_pages.append(page.page_number)
            warnings.extend(page.warnings)
        failed = sum(page.status == PageStatus.FAILED for page in result.pages)
        result.route_summary.failed_pages = failed
        existing_document_warnings = {(warning.code, warning.page_number) for warning in result.warnings}
        result.warnings.extend(
            warning for warning in warnings if (warning.code, warning.page_number) not in existing_document_warnings
        )
        acceptable = failed == 0 and not any(
            warning.severity == WarningSeverity.ERROR or warning.code in {
                "low_text_content",
                "high_replacement_character_ratio",
                "repeated_text",
                "suspicious_unicode_mojibake",
                "suspicious_unicode_glyphs",
                "native_identifier_missing",
                "native_heading_order_mismatch",
            }
            for warning in warnings
        )
        return QualityAssessment(acceptable=acceptable, warnings=warnings, fallback_pages=sorted(set(fallback_pages)))

    evaluate = assess
