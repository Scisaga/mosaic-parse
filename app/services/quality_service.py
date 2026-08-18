"""Conservative, explainable output-quality diagnostics."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.models.parse_result import (
    DocumentParseResult,
    PageParseResult,
    PageSourceKind,
    PageStatus,
    ParseWarning,
    QualitySummary,
    QualityVerdict,
    WarningSeverity,
)
from app.services.evidence_service import number_tokens
from app.utils.settings import setting


@dataclass(slots=True)
class QualityAssessment:
    acceptable: bool
    warnings: list[ParseWarning] = field(default_factory=list)


class QualityService:
    # Some Chinese standards contain a broken ToUnicode map where Latin glyphs
    # decode as a run of CJK characters from the cattle/dog radical blocks,
    # e.g. "Remanufacturing" becomes "犚犲犿犪狀...". Requiring a long run keeps
    # ordinary Chinese words and short standard identifiers such as GB/T out.
    _MOJIBAKE_LATIN_RUN = re.compile(r"(?:[\u7280-\u72ff][ \t]*){6,}")
    _LOW_INFORMATION_REPEAT = re.compile(
        r"^(?:(?:来源|参考)链接|链接)(?:地址)?[：:]?$|^巨潮资讯网[（）()]*$",
        re.IGNORECASE,
    )

    def __init__(self, settings: object | None = None) -> None:
        self.min_page_characters = int(setting(settings, "quality_min_page_characters", 12))
        self.replacement_ratio = float(setting(settings, "quality_max_replacement_ratio", 0.02))
        self.repeat_threshold = int(setting(settings, "quality_repeat_threshold", 5))

    def inspect_page(self, page: PageParseResult) -> list[ParseWarning]:
        content = (page.content or "").strip()
        warnings: list[ParseWarning] = []
        if page.status == PageStatus.FAILED:
            return page.warnings
        if page.diagnostics and page.diagnostics.source_kind == PageSourceKind.SPARSE:
            warnings.append(
                ParseWarning(
                    code="intentional_sparse_page",
                    message="page is visually sparse and its short output agrees with the native PDF evidence",
                    severity=WarningSeverity.INFO,
                    page_number=page.page_number,
                    backend=page.backend,
                    details={
                        "native_text_characters": page.diagnostics.native_text_characters,
                        "visual_ink_ratio": page.diagnostics.visual_ink_ratio,
                    },
                )
            )
        elif len(content) < self.min_page_characters:
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
            # Decimal points and repeated values are expected inside financial
            # tables. Table-specific checks below cover structural repetition;
            # the prose repetition detector must not split table numbers into
            # many identical sentence fragments.
            repeat_source = "\n".join(
                line
                for line in content.splitlines()
                if not line.lstrip().startswith("|") and not line.strip().startswith("<!--")
            )
            repeat_source = re.sub(r"https?://[^\s|）)]+", "", repeat_source, flags=re.IGNORECASE)
            fragments = [
                fragment
                for part in re.split(r"[\n。！？.!?]+", repeat_source)
                if 4 <= len(fragment := part.strip()) <= 120
                and not self._LOW_INFORMATION_REPEAT.fullmatch(re.sub(r"[ \t]+", "", fragment))
                and not re.search(r"(?:https?://|www\.)", fragment, re.IGNORECASE)
            ]
            repeated = [
                (fragment, count)
                for fragment, count in Counter(fragments).items()
                if count >= self.repeat_threshold
            ]
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
            rows = [
                line
                for line in content.splitlines()
                if line.strip().startswith("|") and line.count("|") >= 2
            ]
            outside_table = "\n".join(
                line for line in content.splitlines() if not line.strip().startswith("|")
            )
            table_numbers = Counter(number_tokens("\n".join(rows)))
            outside_numbers = Counter(
                number
                for line in outside_table.splitlines()
                if re.fullmatch(
                    r"[-−]?\(?\d[\d,]*(?:\.\d+)%?\)?",
                    line.strip(),
                )
                and any(marker in line for marker in (".", ",", "%", "−"))
                for number in number_tokens(line)
            )
            unanchored_count = sum((table_numbers & outside_numbers).values())
            if rows and unanchored_count:
                warnings.append(
                    ParseWarning(
                        code="unanchored_table_numbers",
                        message="numbers emitted outside a table are duplicated inside table cells",
                        page_number=page.page_number,
                        backend=page.backend,
                        details={"duplicate_number_count": unanchored_count},
                    )
                )
            for row in rows:
                cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
                populated = [
                    cell
                    for cell in cells
                    if cell
                    and not set(cell) <= {"-", ":", " "}
                    and cell not in {"不适用", "不適用", "N/A", "n/a"}
                    and not re.fullmatch(
                        r"[-−]?\(?\d[\d,]*(?:\.\d+)?%?\)?",
                        cell,
                    )
                ]
                if populated and max(Counter(populated).values()) >= self.repeat_threshold:
                    warnings.append(
                        ParseWarning(
                            code="table_header_propagation",
                            message="a table row repeats the same non-empty cell across too many columns",
                            page_number=page.page_number,
                            backend=page.backend,
                        )
                    )
                    break
            if rows and max(row.count("|") - 1 for row in rows) > 24:
                warnings.append(
                    ParseWarning(
                        code="table_shape_explosion",
                        message="a rendered table has an implausibly large number of columns",
                        page_number=page.page_number,
                        backend=page.backend,
                    )
                )
        diagnostics = page.diagnostics
        if diagnostics and diagnostics.detected_rotation_degrees in {90, 180, 270}:
            warnings.append(
                ParseWarning(
                    code="rotated_scan",
                    message="page image content requires orientation normalization before reliable OCR",
                    severity=WarningSeverity.INFO,
                    page_number=page.page_number,
                    backend=page.backend,
                    details={"rotation_degrees": diagnostics.detected_rotation_degrees},
                )
            )
        if (
            diagnostics
            and diagnostics.source_kind == PageSourceKind.NATIVE
            and diagnostics.selected_strategy.value == "docling"
            and diagnostics.native_text_characters
            and len(re.sub(r"\s+", "", content)) < diagnostics.native_text_characters * 0.20
        ):
            warnings.append(
                ParseWarning(
                    code="visual_text_mismatch",
                    message="parsed output contains far less text than the measured native PDF layer",
                    page_number=page.page_number,
                    backend=page.backend,
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
            if page.status != PageStatus.FAILED
            and cls.has_suspicious_unicode_mojibake(page.content)
        ]

    def assess(self, result: DocumentParseResult) -> QualityAssessment:
        warnings: list[ParseWarning] = []
        for page in result.pages:
            if page.status != PageStatus.FAILED and not (page.content or "").strip():
                if not any(warning.code == "no_usable_content" for warning in page.warnings):
                    page.warnings.append(
                        ParseWarning(
                            code="no_usable_content",
                            message="page has no usable parsed content",
                            severity=WarningSeverity.ERROR,
                            page_number=page.page_number,
                            backend=page.backend,
                        )
                    )
                page.status = PageStatus.FAILED
            detected = self.inspect_page(page)
            existing_codes = {warning.code for warning in page.warnings}
            page.warnings.extend(
                warning for warning in detected if warning.code not in existing_codes
            )
            actionable = [
                warning for warning in page.warnings if warning.severity != WarningSeverity.INFO
            ]
            if actionable and page.status == PageStatus.COMPLETED:
                page.status = PageStatus.WARNING
            warnings.extend(page.warnings)
            if page.diagnostics is not None:
                visual = page.diagnostics.visual_fusion
                warning_codes = {warning.code for warning in page.warnings}
                if visual is not None:
                    if page.status == PageStatus.FAILED or warning_codes & {
                        "qwen_response_truncated",
                        "visual_fusion_partial",
                        "visual_fusion_timeout",
                        "table_structure_invalid",
                    }:
                        page.diagnostics.quality_verdict = QualityVerdict.UNTRUSTED
                    elif (
                        visual.unresolved_conflicts or 0
                    ) > 0 or page.status == PageStatus.WARNING:
                        page.diagnostics.quality_verdict = QualityVerdict.DEGRADED
                    else:
                        page.diagnostics.quality_verdict = QualityVerdict.TRUSTED
                elif page.status in {PageStatus.WARNING, PageStatus.FAILED} or any(
                    warning.code
                    in {
                        "low_text_content",
                        "high_replacement_character_ratio",
                        "repeated_text",
                        "suspicious_unicode_mojibake",
                        "suspicious_unicode_glyphs",
                        "native_identifier_missing",
                        "native_heading_order_mismatch",
                        "reading_order_inversion",
                        "table_shape_explosion",
                        "table_header_propagation",
                        "unanchored_table_numbers",
                        "visual_text_mismatch",
                    }
                    for warning in page.warnings
                ):
                    page.diagnostics.quality_verdict = QualityVerdict.UNTRUSTED
                else:
                    page.diagnostics.quality_verdict = QualityVerdict.TRUSTED
        failed = sum(page.status == PageStatus.FAILED for page in result.pages)
        result.route_summary.failed_pages = failed
        existing_document_warnings = {
            (warning.code, warning.page_number) for warning in result.warnings
        }
        result.warnings.extend(
            warning
            for warning in warnings
            if (warning.code, warning.page_number) not in existing_document_warnings
        )
        acceptable = failed == 0 and not any(
            warning.severity == WarningSeverity.ERROR
            or warning.code
            in {
                "low_text_content",
                "high_replacement_character_ratio",
                "repeated_text",
                "suspicious_unicode_mojibake",
                "suspicious_unicode_glyphs",
                "native_identifier_missing",
                "native_heading_order_mismatch",
                "reading_order_inversion",
                "table_shape_explosion",
                "table_header_propagation",
                "unanchored_table_numbers",
                "visual_text_mismatch",
                "qwen_response_truncated",
                "visual_fusion_partial",
                "visual_fusion_timeout",
                "unresolved_visual_conflict",
            }
            for warning in warnings
        )
        self.summarize(result)
        return QualityAssessment(acceptable=acceptable, warnings=warnings)

    @staticmethod
    def summarize(result: DocumentParseResult) -> QualitySummary:
        summary = QualitySummary()
        for page in result.pages:
            diagnostics = page.diagnostics
            verdict = (
                diagnostics.quality_verdict
                if diagnostics
                else (
                    QualityVerdict.UNTRUSTED
                    if page.status in {PageStatus.WARNING, PageStatus.FAILED}
                    else QualityVerdict.TRUSTED
                )
            )
            if verdict == QualityVerdict.TRUSTED:
                summary.trusted_pages += 1
            elif verdict == QualityVerdict.DEGRADED:
                summary.degraded_pages += 1
            else:
                summary.untrusted_pages += 1
            if diagnostics and diagnostics.selected_strategy.value != "docling":
                summary.repaired_pages += 1
            if diagnostics:
                visual = diagnostics.visual_fusion
                if visual is not None:
                    summary.visual_pages += 1
                    summary.qwen_calls += visual.qwen_calls or 0
                    summary.qwen_resolved_conflicts += visual.qwen_resolved_conflicts or 0
                    summary.unresolved_visual_conflicts += visual.unresolved_conflicts or 0
        result.quality_summary = summary
        return summary

    evaluate = assess
