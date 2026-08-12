"""Parser coordination, timeout handling, normalization, and conservative fallback."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.models.backend import BackendStatus
from app.models.error import ServiceError
from app.models.parse_options import DocumentParseOptions, ParseMode
from app.models.parse_result import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParseWarning,
    WarningSeverity,
)
from app.models.source import StoredSource
from app.parsers import (
    DoclingStandardParser,
    GlmOcrRemoteAdapter,
    OllamaVlmParser,
    ParserCancelledError,
    ParserError,
    ParserRegistry,
    ParserUnavailableError,
    ProgressCallback,
)
from app.parsers.docling_standard import PictureCandidate
from app.security.file_validation import FileValidationError, validate_stored_file
from app.services.export_service import ExportService
from app.services.quality_service import QualityService
from app.utils.ids import new_document_id
from app.utils.page_range import PageRangeError, format_page_range, parse_page_range
from app.utils.settings import setting


@dataclass(slots=True)
class _NativeIdentifierEvidence:
    identifier: str
    prefix: str
    suffix: str


@dataclass(slots=True)
class _NativePageEvidence:
    words: set[str] = field(default_factory=set)
    compounds: set[str] = field(default_factory=set)
    cjk_space_boundaries: set[str] = field(default_factory=set)
    underscore_identifiers: list[_NativeIdentifierEvidence] = field(default_factory=list)
    numbered_headings: dict[str, str] = field(default_factory=dict)


class ParserService:
    _DIAGRAM_HINT = re.compile(r"流程图|工作流|框图|flow\s*chart|workflow|diagram", re.IGNORECASE)
    _IMAGE_PLACEHOLDER = "<!-- image -->"
    _CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    _CJK_SPACE = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
    _SPACE_BEFORE_CJK_PUNCT = re.compile(r"[ \t]+(?=[，。！？；：、）》】」』])")
    _SPACE_AFTER_CJK_PUNCT = re.compile(r"(?<=[（《【「『，。！？；：、])[ \t]+")
    _ALNUM_RUN = re.compile(r"[A-Za-z0-9]+(?:[ \t]+[A-Za-z0-9]+)+")
    _COMPOUND_RUN = re.compile(r"[A-Za-z0-9]+(?:[ \t]*[./:+-][ \t]*[A-Za-z0-9]+)+")
    _INLINE_CODE = re.compile(r"(`+)([^\n]*?)\1")
    _NATIVE_WORD = re.compile(r"[A-Za-z0-9]+")
    _NATIVE_COMPOUND = re.compile(r"[A-Za-z0-9]+(?:[./:+-][A-Za-z0-9]+)+")
    _NATIVE_UNDERSCORE_IDENTIFIER = re.compile(
        r"(?<![A-Za-z0-9_])(?P<identifier>[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)(?![A-Za-z0-9_])"
    )
    _NATIVE_NUMBERED_HEADING = re.compile(
        r"(?m)^[ \t]*(?P<section>\d+(?:\.\d+)+\.?)[ \t]+(?P<title>[^\n]{2,200})[ \t]*$"
    )
    _MARKDOWN_NUMBERED_HEADING = re.compile(
        r"(?m)^[ \t]*#{1,6}[ \t]+(?P<section>\d+(?:\.\d+)+\.?)[ \t]+(?P<title>[^\n]{2,200})[ \t]*$"
    )
    _CONTEXT_CHARACTER = re.compile(rf"[A-Za-z0-9{_CJK}]")
    _BROKEN_STANDARD_PREFIX = re.compile(r"犌[ \t　]*犅[ \t　]*[／/][ \t　]*犜")
    _BROKEN_APPENDIX_CONTEXT = re.compile(
        r"(?P<prefix>(?:附[ \t　]*录|图|表)[ \t　]*)(?P<glyph>[犃犅犆犇])"
    )
    _BROKEN_APPENDIX_HEADING = re.compile(
        r"(?m)^(?P<prefix>[ \t]*(?:#{1,6}[ \t]*)?)(?P<glyph>[犃犅犆犇])(?=[ \t]*(?:[．.]|[０-９0-9]))"
    )
    _BROKEN_APPENDIX_LETTERS = {"犃": "A", "犅": "B", "犆": "C", "犇": "D"}
    _SEVERE_SPACING_MIN_REMOVED = 20
    _SEVERE_SPACING_MIN_RATIO = 0.08

    def __init__(
        self,
        settings: object | None,
        registry: ParserRegistry | None = None,
        quality_service: QualityService | None = None,
        export_service: ExportService | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry or ParserRegistry()
        self.quality_service = quality_service or QualityService(settings)
        self.export_service = export_service or ExportService()
        self._semaphore = asyncio.Semaphore(int(setting(settings, "parser_workers", 1)))
        self._lifecycle_condition = asyncio.Condition()
        self._reloading = False
        self._active_parses = 0
        self._initialized = False
        self._build_adapters()

    def _build_adapters(self) -> None:
        self.glm_adapter = GlmOcrRemoteAdapter(self.settings)
        self.standard_parser = DoclingStandardParser(self.settings, self.glm_adapter)
        self.vlm_parser = OllamaVlmParser(self.settings)
        self.registry.clear()
        self.registry.register(ParseMode.AUTO, self.standard_parser)
        self.registry.register(ParseMode.STANDARD, self.standard_parser)
        self.registry.register(ParseMode.OCR, self.standard_parser)
        self.registry.register(ParseMode.VLM, self.vlm_parser)

    async def initialize(self) -> None:
        if self._initialized:
            return
        await asyncio.gather(
            self.standard_parser.initialize(),
            self.vlm_parser.initialize(),
        )
        # A missing optional backend must not prevent CPU/native-text startup.
        self._initialized = True

    async def reload(self) -> None:
        await self._begin_exclusive_lifecycle()
        try:
            await self._close_adapters()
            self.registry = ParserRegistry()
            self._build_adapters()
            await self.initialize()
        finally:
            await self._end_exclusive_lifecycle()

    async def close(self) -> None:
        await self._begin_exclusive_lifecycle()
        try:
            await self._close_adapters()
        finally:
            await self._end_exclusive_lifecycle()

    async def _close_adapters(self) -> None:
        await asyncio.gather(
            self.standard_parser.close(),
            self.glm_adapter.close(),
            self.vlm_parser.close(),
            return_exceptions=True,
        )
        self._initialized = False

    async def _begin_exclusive_lifecycle(self) -> None:
        async with self._lifecycle_condition:
            while self._reloading:
                await self._lifecycle_condition.wait()
            self._reloading = True
            try:
                while self._active_parses:
                    await self._lifecycle_condition.wait()
            except BaseException:
                # A cancelled reload/close must not leave the admission gate
                # closed forever while it was waiting for an active parse.
                self._reloading = False
                self._lifecycle_condition.notify_all()
                raise

    async def _end_exclusive_lifecycle(self) -> None:
        async with self._lifecycle_condition:
            self._reloading = False
            self._lifecycle_condition.notify_all()

    async def _enter_parse(self) -> None:
        async with self._lifecycle_condition:
            while self._reloading:
                await self._lifecycle_condition.wait()
            self._active_parses += 1

    async def _leave_parse(self) -> None:
        async with self._lifecycle_condition:
            self._active_parses -= 1
            if self._active_parses == 0:
                self._lifecycle_condition.notify_all()

    async def probe_backends(self) -> list[BackendStatus]:
        if not self._initialized:
            await self.initialize()
        docling, glm, vlm = await asyncio.gather(
            self.standard_parser.probe(),
            self.glm_adapter.probe(),
            self.vlm_parser.probe(),
        )
        return [docling, glm, vlm]

    def _coerce_source(self, source: StoredSource | str | Path) -> StoredSource:
        if isinstance(source, StoredSource):
            return source
        path = Path(source)
        try:
            filename, mime_type, size, page_count = validate_stored_file(
                path,
                path.name,
                max_bytes=int(setting(self.settings, "max_upload_bytes", 200 * 1024 * 1024)),
                max_pages=int(setting(self.settings, "max_document_pages", 1_000)),
            )
        except FileValidationError as exc:
            raise ServiceError(exc.code, str(exc), status_code=415) from exc
        return StoredSource(
            path=path,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size,
            page_count=page_count,
        )

    def _finalize_exports(self, result: DocumentParseResult, options: DocumentParseOptions) -> None:
        markdown, plain_text = self.export_service.join_pages(
            result.pages,
            preserve_page_breaks=options.preserve_page_breaks,
        )
        result.markdown = markdown
        result.plain_text = plain_text
        result.processed_pages = sum(page.status.value != "failed" for page in result.pages)

    @classmethod
    def _native_lexicons(
        cls,
        source: StoredSource,
        page_numbers: set[int],
    ) -> dict[int, _NativePageEvidence]:
        if source.mime_type != "application/pdf":
            return {}
        try:
            import pymupdf
        except ImportError:
            try:
                import fitz as pymupdf  # type: ignore[no-redef]
            except ImportError:
                return {}
        lexicons: dict[int, _NativePageEvidence] = {}
        try:
            with pymupdf.open(source.path) as document:
                for page_number in page_numbers:
                    if not 1 <= page_number <= document.page_count:
                        continue
                    evidence = _NativePageEvidence()
                    native_page = document.load_page(page_number - 1)
                    for item in native_page.get_text("words"):
                        token = str(item[4])
                        evidence.words.update(match.group().casefold() for match in cls._NATIVE_WORD.finditer(token))
                        evidence.compounds.update(
                            match.group().casefold() for match in cls._NATIVE_COMPOUND.finditer(token)
                        )
                    native_text = str(native_page.get_text("text"))
                    for match in cls._CJK_SPACE.finditer(native_text):
                        evidence.cjk_space_boundaries.add(
                            native_text[match.start() - 1] + native_text[match.end()]
                        )
                    for match in cls._NATIVE_UNDERSCORE_IDENTIFIER.finditer(native_text):
                        identifier = match.group("identifier")
                        if len(identifier) < 6:
                            continue
                        prefix = "".join(cls._CONTEXT_CHARACTER.findall(native_text[: match.start()]))[-6:]
                        suffix = "".join(cls._CONTEXT_CHARACTER.findall(native_text[match.end() :]))[:6]
                        if len(prefix) >= 4 and len(suffix) >= 4:
                            evidence.underscore_identifiers.append(
                                _NativeIdentifierEvidence(
                                    identifier=identifier,
                                    prefix=prefix,
                                    suffix=suffix,
                                )
                            )
                    sorted_native_text = str(native_page.get_text("text", sort=True))
                    evidence.numbered_headings = {
                        match.group("section").rstrip("."): match.group("title").strip()
                        for match in cls._NATIVE_NUMBERED_HEADING.finditer(sorted_native_text)
                    }
                    lexicons[page_number] = evidence
        except (OSError, RuntimeError, ValueError):
            return {}
        return lexicons

    @staticmethod
    def _merge_native_fragments(match: re.Match[str], native_words: set[str]) -> str:
        parts = re.findall(r"[A-Za-z0-9]+", match.group())
        merged: list[str] = []
        index = 0
        while index < len(parts):
            match_end = index + 1
            for end in range(len(parts), index + 1, -1):
                if "".join(parts[index:end]).casefold() in native_words:
                    match_end = end
                    break
            merged.append("".join(parts[index:match_end]))
            index = match_end
        return " ".join(merged)

    @staticmethod
    def _merge_native_compound(match: re.Match[str], native_compounds: set[str]) -> str:
        original = match.group()
        compact = re.sub(r"[ \t]+", "", original)
        return compact if compact.casefold() in native_compounds else original

    @staticmethod
    def _suspicious_glyph_counts(text: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for character in text:
            # U+F0B7 is the one confirmed Wingdings bullet mapping. It is
            # reported as mapped only when sanitation actually touches it;
            # protected code spans retain it verbatim and do not report it as
            # an unknown private-use glyph.
            if character in {"\n", "\t", "\uf0b7"}:
                continue
            if character == "\ufffd" or unicodedata.category(character) in {"Cc", "Cf", "Co", "Cs"}:
                codepoint = f"U+{ord(character):04X}"
                counts[codepoint] = counts.get(codepoint, 0) + 1
        return counts

    @staticmethod
    def _sanitize_glyphs(text: str) -> tuple[str, dict[str, int]]:
        counts = ParserService._suspicious_glyph_counts(text)
        output: list[str] = []
        for character in text:
            if character in {"\n", "\t"}:
                output.append(character)
                continue
            if character == "\uf0b7":
                counts["U+F0B7"] = counts.get("U+F0B7", 0) + 1
                output.append("•")
            else:
                output.append(character)
        cleaned = "".join(output)
        # Docling already emits a Markdown list marker for Wingdings bullets.
        cleaned = re.sub(r"(?m)^([ \t]*[-*+][ \t]+)•[ \t]*", r"\1", cleaned)
        return cleaned, counts

    @classmethod
    def _normalize_text_segment(
        cls,
        text: str,
        native_words: set[str],
        native_compounds: set[str],
        native_cjk_space_boundaries: set[str],
    ) -> str:
        text = cls._CJK_SPACE.sub(
            lambda match: (
                " "
                if match.string[match.start() - 1] + match.string[match.end()] in native_cjk_space_boundaries
                else ""
            ),
            text,
        )
        text = cls._SPACE_BEFORE_CJK_PUNCT.sub("", text)
        text = cls._SPACE_AFTER_CJK_PUNCT.sub("", text)
        text = cls._ALNUM_RUN.sub(lambda match: cls._merge_native_fragments(match, native_words), text)
        return cls._COMPOUND_RUN.sub(
            lambda match: cls._merge_native_compound(match, native_compounds),
            text,
        )

    @classmethod
    def _normalize_markdown_text(
        cls,
        text: str,
        native_words: set[str],
        native_compounds: set[str],
        native_cjk_space_boundaries: set[str] | None = None,
    ) -> tuple[str, dict[str, int]]:
        cjk_space_boundaries = native_cjk_space_boundaries or set()
        output: list[str] = []
        glyphs: dict[str, int] = {}
        fence: str | None = None
        for line in text.splitlines(keepends=True):
            stripped = line.lstrip()
            marker = stripped[:3]
            if fence is not None:
                output.append(line)
                for codepoint, count in cls._suspicious_glyph_counts(line).items():
                    glyphs[codepoint] = glyphs.get(codepoint, 0) + count
                if stripped.startswith(fence):
                    fence = None
                continue
            if marker in {"```", "~~~"}:
                fence = marker
                output.append(line)
                continue
            parts: list[str] = []
            cursor = 0
            for match in cls._INLINE_CODE.finditer(line):
                segment, counts = cls._sanitize_glyphs(line[cursor : match.start()])
                parts.append(
                    cls._normalize_text_segment(
                        segment,
                        native_words,
                        native_compounds,
                        cjk_space_boundaries,
                    )
                )
                for codepoint, count in counts.items():
                    glyphs[codepoint] = glyphs.get(codepoint, 0) + count
                parts.append(match.group())
                for codepoint, count in cls._suspicious_glyph_counts(match.group()).items():
                    glyphs[codepoint] = glyphs.get(codepoint, 0) + count
                cursor = match.end()
            segment, counts = cls._sanitize_glyphs(line[cursor:])
            parts.append(
                cls._normalize_text_segment(
                    segment,
                    native_words,
                    native_compounds,
                    cjk_space_boundaries,
                )
            )
            for codepoint, count in counts.items():
                glyphs[codepoint] = glyphs.get(codepoint, 0) + count
            output.append("".join(parts))
        return "".join(output), glyphs

    @classmethod
    def _add_native_residual_warnings(
        cls,
        page: PageParseResult,
        evidence: _NativePageEvidence,
    ) -> None:
        content = page.content or ""
        compact_content = re.sub(r"\s+", "", content).replace(r"\_", "_").casefold()
        context_content = "".join(cls._CONTEXT_CHARACTER.findall(content)).casefold()
        missing_identifiers = sorted(
            {
                item.identifier
                for item in evidence.underscore_identifiers
                if item.identifier.casefold() not in compact_content
                and item.prefix.casefold() in context_content
                and item.suffix.casefold() in context_content
            }
        )
        if missing_identifiers and not any(
            warning.code == "native_identifier_missing" for warning in page.warnings
        ):
            page.warnings.append(
                ParseWarning(
                    code="native_identifier_missing",
                    message="one or more long underscore identifiers present in the native PDF text are missing",
                    page_number=page.page_number,
                    backend=page.backend,
                    details={
                        "identifiers": missing_identifiers,
                        "minimum_length": 6,
                        "same_page_context_verified": True,
                    },
                )
            )

        parsed_headings = {
            match.group("section").rstrip("."): match.group("title").strip()
            for match in cls._MARKDOWN_NUMBERED_HEADING.finditer(content)
        }
        heading_mismatches: list[dict[str, str]] = []
        for section, native_title in evidence.numbered_headings.items():
            parsed_title = parsed_headings.get(section)
            if parsed_title is None:
                continue
            native_compact = re.sub(r"\s+", "", native_title).casefold()
            parsed_compact = re.sub(r"\s+", "", parsed_title).casefold()
            if native_compact == parsed_compact or sorted(native_compact) != sorted(parsed_compact):
                continue
            heading_mismatches.append(
                {
                    "section": section,
                    "native_heading": native_title,
                    "parsed_heading": parsed_title,
                }
            )
        if heading_mismatches and not any(
            warning.code == "native_heading_order_mismatch" for warning in page.warnings
        ):
            page.warnings.append(
                ParseWarning(
                    code="native_heading_order_mismatch",
                    message="a numbered heading differs from the native PDF visual-order heading after whitespace removal",
                    page_number=page.page_number,
                    backend=page.backend,
                    details={
                        "mismatches": heading_mismatches,
                        "comparison": "same_section_same_characters_different_order_after_whitespace_removed",
                    },
                )
            )

    async def _normalize_auto_text(self, result: DocumentParseResult, source: StoredSource) -> None:
        pages = {page.page_number for page in result.pages if page.status != PageStatus.FAILED}
        lexicons = await asyncio.to_thread(self._native_lexicons, source, pages)
        changed_pages: set[int] = set()
        suspicious_glyph_pages: set[int] = set()
        suspicious_glyphs_by_page: dict[int, dict[str, int]] = {}
        severely_polluted_pages: dict[int, tuple[int, float]] = {}
        sanitized: dict[str, int] = {}
        for page in result.pages:
            evidence = lexicons.get(page.page_number, _NativePageEvidence())
            for attribute in ("content", "plain_text"):
                original = getattr(page, attribute)
                if original is None:
                    continue
                normalized, glyphs = self._normalize_markdown_text(
                    original,
                    evidence.words,
                    evidence.compounds,
                    evidence.cjk_space_boundaries,
                )
                if normalized != original:
                    changed_pages.add(page.page_number)
                    setattr(page, attribute, normalized)
                # Markdown content is the canonical persisted representation;
                # do not double-count the same glyphs in its plain-text export.
                if attribute == "content" or page.content is None:
                    for codepoint, count in glyphs.items():
                        sanitized[codepoint] = sanitized.get(codepoint, 0) + count
                    suspicious = {codepoint: count for codepoint, count in glyphs.items() if codepoint != "U+F0B7"}
                    if suspicious:
                        suspicious_glyph_pages.add(page.page_number)
                        suspicious_glyphs_by_page[page.page_number] = suspicious
                    removed_spaces = max(
                        0,
                        original.count(" ")
                        + original.count("\t")
                        - normalized.count(" ")
                        - normalized.count("\t"),
                    )
                    visible_characters = max(1, sum(not character.isspace() for character in original))
                    removed_ratio = removed_spaces / visible_characters
                    if (
                        removed_spaces >= self._SEVERE_SPACING_MIN_REMOVED
                        and removed_ratio >= self._SEVERE_SPACING_MIN_RATIO
                    ):
                        severely_polluted_pages[page.page_number] = (removed_spaces, removed_ratio)
            self._add_native_residual_warnings(page, evidence)
        for page in result.pages:
            page_suspicious = suspicious_glyphs_by_page.get(page.page_number)
            if page_suspicious is not None and not any(
                warning.code == "suspicious_unicode_glyphs" for warning in page.warnings
            ):
                page.warnings.append(
                    ParseWarning(
                        code="suspicious_unicode_glyphs",
                        message="page contains preserved Unicode replacement, control, format, private-use, or surrogate glyphs",
                        page_number=page.page_number,
                        backend=page.backend,
                        details={"detected_codepoints": page_suspicious, "characters_preserved": True},
                    )
                )
        if changed_pages or suspicious_glyph_pages:
            details: dict[str, object] = {
                "pages": sorted(changed_pages),
                "suspicious_glyph_pages": sorted(suspicious_glyph_pages),
                "detected_codepoints": sanitized,
                "mapped_codepoints": ({"U+F0B7": sanitized["U+F0B7"]} if "U+F0B7" in sanitized else {}),
                "native_token_guard": source.mime_type == "application/pdf",
            }
            if severely_polluted_pages:
                details["severe_spacing_pollution"] = {
                    "pages": sorted(severely_polluted_pages),
                    "metrics": [
                        {
                            "page_number": page_number,
                            "removed_spaces": metrics[0],
                            "removed_to_visible_ratio": round(metrics[1], 4),
                            "layout_order_repaired": False,
                        }
                        for page_number, metrics in sorted(severely_polluted_pages.items())
                    ],
                }
            result.warnings.append(
                ParseWarning(
                    code="auto_text_normalized",
                    message=(
                        f"conservative text normalization changed {len(changed_pages)} page(s); "
                        f"suspicious glyphs were observed on {len(suspicious_glyph_pages)} page(s)"
                    ),
                    severity=WarningSeverity.INFO,
                    backend=self.standard_parser.name,
                    details=details,
                )
            )

    @classmethod
    def _normalize_auto_short_mapped_tokens(cls, result: DocumentParseResult) -> None:
        """Normalize only short broken-ToUnicode tokens with strong context.

        Single mapped glyphs can legitimately be CJK text, so this deliberately
        avoids a global substitution.  It covers standard identifiers and
        appendix/table/figure labels whose surrounding syntax makes A-D or
        ``GB/T`` unambiguous.
        """

        changed_pages: set[int] = set()

        def normalize(value: str | None) -> str | None:
            if value is None:
                return None
            normalized = cls._BROKEN_STANDARD_PREFIX.sub("GB/T", value)
            normalized = cls._BROKEN_APPENDIX_CONTEXT.sub(
                lambda match: f"{match.group('prefix')}{cls._BROKEN_APPENDIX_LETTERS[match.group('glyph')]}",
                normalized,
            )
            return cls._BROKEN_APPENDIX_HEADING.sub(
                lambda match: f"{match.group('prefix')}{cls._BROKEN_APPENDIX_LETTERS[match.group('glyph')]}",
                normalized,
            )

        for page in result.pages:
            content = normalize(page.content)
            plain_text = normalize(page.plain_text)
            if content != page.content or plain_text != page.plain_text:
                page.content = content
                page.plain_text = plain_text
                changed_pages.add(page.page_number)
        if changed_pages:
            result.warnings.append(
                ParseWarning(
                    code="auto_contextual_unicode_normalized",
                    message=f"normalized contextual broken-ToUnicode labels on {len(changed_pages)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend="docling-standard",
                    details={
                        "pages": sorted(changed_pages),
                        "strategy": "contextual_standard_and_appendix_labels",
                    },
                )
            )

    @staticmethod
    async def _notify_postprocess(
        callback: ProgressCallback | None,
        current: int,
        total: int,
        phase: str,
    ) -> None:
        if callback is None:
            return
        notification = callback(current, total, phase)
        if inspect.isawaitable(notification):
            await notification

    @staticmethod
    def _usable_replacements(
        replacement: DocumentParseResult,
        requested_pages: set[int],
    ) -> dict[int, PageParseResult]:
        return {
            page.page_number: page
            for page in replacement.pages
            if page.page_number in requested_pages
            and page.status != PageStatus.FAILED
            and bool((page.content or "").strip())
            and not QualityService.has_suspicious_unicode_mojibake(page.content)
        }

    async def _repair_auto_mojibake(
        self,
        result: DocumentParseResult,
        source: StoredSource,
        options: DocumentParseOptions,
        pages: list[int],
        *,
        document_id: str,
        cancel_event: object | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        """Repair only confidently detected broken-ToUnicode pages.

        Full-page GLM OCR is preferred because it is a transcription backend;
        whole-page VLM replacement remains governed by the existing explicit
        fallback option.
        """

        unresolved = set(pages)
        await self._notify_postprocess(
            progress_callback,
            result.processed_pages,
            len(result.pages),
            "postprocess.text_repair",
        )
        replacements: dict[int, PageParseResult] = {}
        used_ocr: set[int] = set()
        glm_status = await self.glm_adapter.probe()
        if glm_status.ready:
            ocr_options = options.model_copy(
                update={
                    "mode": ParseMode.OCR,
                    "page_range": format_page_range(pages),
                    "enable_vlm_fallback": False,
                }
            )
            try:
                repaired = await self.standard_parser.parse(
                    source,
                    ocr_options,
                    document_id=document_id,
                    cancel_event=cancel_event,
                )
                accepted = self._usable_replacements(repaired, unresolved)
                replacements.update(accepted)
                used_ocr.update(accepted)
                unresolved.difference_update(accepted)
            except (ParserCancelledError, asyncio.CancelledError):
                raise
            except ParserError:
                # AUTO is fail-soft: a secondary correction route must never
                # discard the usable primary Docling result.
                pass

        if replacements:
            result.pages = [replacements.get(page.page_number, page) for page in result.pages]
        if used_ocr:
            result.route_summary.pages_with_ocr = len(used_ocr)
            result.route_summary.ocr_regions = len(used_ocr)
            result.warnings.append(
                ParseWarning(
                    code="auto_mojibake_repaired",
                    message=f"full-page OCR repaired suspicious Unicode text on {len(used_ocr)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend=self.glm_adapter.name,
                    details={"pages": sorted(used_ocr), "strategy": "full_page_ocr"},
                )
            )
        for page in result.pages:
            if page.page_number not in unresolved:
                continue
            page.status = PageStatus.WARNING
            if not any(warning.code == "suspicious_unicode_mojibake" for warning in page.warnings):
                page.warnings.append(
                    ParseWarning(
                        code="suspicious_unicode_mojibake",
                        message="page contains a likely broken PDF ToUnicode mapping after the OCR repair attempt",
                        page_number=page.page_number,
                        backend=page.backend,
                    )
                )

    @classmethod
    def _insert_after_picture_placeholder(cls, content: str, index: int, mermaid: str) -> str | None:
        matches = list(re.finditer(re.escape(cls._IMAGE_PLACEHOLDER), content))
        if not 0 <= index < len(matches):
            return None
        insertion = matches[index].end()
        return f"{content[:insertion]}\n\n{mermaid}{content[insertion:]}"

    async def _enrich_auto_diagrams(
        self,
        result: DocumentParseResult,
        source: StoredSource,
        options: DocumentParseOptions,
        *,
        cancel_event: object | None,
        progress_callback: ProgressCallback | None,
    ) -> None:
        if not bool(setting(self.settings, "vlm_diagram_enrichment_enabled", True)):
            return
        # A deliberately disabled optional backend is not a degraded runtime
        # state and should not add one warning per otherwise valid diagram.
        if getattr(self.vlm_parser, "enabled", None) is False:
            return
        candidates = [candidate for candidate in result._picture_candidates if isinstance(candidate, PictureCandidate)]
        page_by_number = {page.page_number: page for page in result.pages}
        candidates = [
            candidate
            for candidate in candidates
            if candidate.page_number in page_by_number and self._DIAGRAM_HINT.search(candidate.caption)
        ]
        if not candidates:
            return
        await self._notify_postprocess(
            progress_callback,
            result.processed_pages,
            len(result.pages),
            "postprocess.diagram",
        )
        status = await self.vlm_parser.probe()
        if not status.ready:
            result.warnings.append(
                ParseWarning(
                    code="diagram_enrichment_unavailable",
                    message="a diagram was retained as an image placeholder because the VLM backend was unavailable",
                    backend=self.vlm_parser.name,
                    details={
                        "pages": sorted({candidate.page_number for candidate in candidates}),
                        "source_retained": "markdown_image_placeholder",
                    },
                )
            )
            return

        enriched_pages: set[int] = set()
        for candidate in candidates:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                raise ParserCancelledError("job was cancelled")
            page = page_by_number[candidate.page_number]
            try:
                mermaid = await self.vlm_parser.diagram_to_mermaid(
                    source,
                    page_number=candidate.page_number,
                    normalized_bbox=candidate.normalized_bbox,
                    caption=candidate.caption,
                    languages=options.language,
                    profile=options.profile.value,
                )
                enriched = self._insert_after_picture_placeholder(
                    page.content or "",
                    candidate.placeholder_index,
                    mermaid,
                )
                if enriched is None:
                    raise ParserError("diagram placeholder no longer matches Docling reading order")
                page.content = enriched
                enriched_pages.add(candidate.page_number)
            except (ParserCancelledError, asyncio.CancelledError):
                raise
            except Exception as exc:
                result.warnings.append(
                    ParseWarning(
                        code="diagram_enrichment_failed",
                        message="a diagram was retained as an image placeholder because strict Mermaid generation failed",
                        page_number=candidate.page_number,
                        backend=self.vlm_parser.name,
                        details={
                            "reason": type(exc).__name__,
                            "source_retained": "markdown_image_placeholder",
                        },
                    )
                )

        if enriched_pages:
            result.pipeline.vlm = self.vlm_parser.name
            result._vlm_page_numbers.update(enriched_pages)
            result.route_summary.vlm_pages = len(result._vlm_page_numbers)
            result.warnings.append(
                ParseWarning(
                    code="diagram_mermaid_generated",
                    message=f"VLM generated validated Mermaid for diagrams on {len(enriched_pages)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend=self.vlm_parser.name,
                    details={
                        "pages": sorted(enriched_pages),
                        "derived": True,
                        "source_retained": "original_document_and_markdown_image_placeholder",
                        "validation": "strict_flowchart_with_edges",
                        "crops": [
                            {
                                "page_number": candidate.page_number,
                                "normalized_bbox": list(candidate.normalized_bbox),
                            }
                            for candidate in candidates
                            if candidate.page_number in enriched_pages
                        ],
                    },
                )
            )

    async def _apply_vlm_fallback(
        self,
        result: DocumentParseResult,
        source: StoredSource,
        options: DocumentParseOptions,
        fallback_pages: list[int],
        *,
        document_id: str,
        cancel_event: object | None,
    ) -> None:
        status = await self.vlm_parser.probe()
        if not status.ready:
            result.warnings.append(
                ParseWarning(
                    code="vlm_fallback_unavailable",
                    message="quality fallback was requested but the VLM backend is unavailable",
                    backend=self.vlm_parser.name,
                )
            )
            return
        fallback_options = options.model_copy(
            update={
                "mode": ParseMode.VLM,
                "page_range": format_page_range(fallback_pages),
                "enable_vlm_fallback": False,
            }
        )
        try:
            fallback = await self.vlm_parser.parse(
                source,
                fallback_options,
                document_id=document_id,
                cancel_event=cancel_event,
            )
        except ParserError as exc:
            result.warnings.append(
                ParseWarning(
                    code="vlm_fallback_failed",
                    message=f"VLM fallback failed: {type(exc).__name__}",
                    backend=self.vlm_parser.name,
                )
            )
            return
        replacements = self._usable_replacements(fallback, set(fallback_pages))
        if not replacements:
            return
        resolved_page_warnings = [
            warning
            for page in result.pages
            if page.page_number in replacements
            for warning in page.warnings
        ]
        mirrored_warning_counts = Counter(
            self._warning_fingerprint(warning) for warning in resolved_page_warnings
        )
        retained_document_warnings: list[ParseWarning] = []
        for warning in result.warnings:
            fingerprint = self._warning_fingerprint(warning)
            if mirrored_warning_counts[fingerprint] > 0:
                mirrored_warning_counts[fingerprint] -= 1
            else:
                retained_document_warnings.append(warning)
        result.warnings = retained_document_warnings
        result.pages = [replacements.get(page.page_number, page) for page in result.pages]
        result.pipeline.vlm = self.vlm_parser.name
        result._vlm_page_numbers.update(replacements)
        result.route_summary.vlm_pages = len(result._vlm_page_numbers)
        result.warnings.append(
            ParseWarning(
                code="vlm_fallback_used",
                message=f"VLM fallback replaced {len(replacements)} page(s) after quality checks",
                severity=WarningSeverity.INFO,
                backend=self.vlm_parser.name,
                details={
                    "pages": sorted(replacements),
                    "resolved_warnings": [
                        {"code": code, "page_number": page_number}
                        for code, page_number in sorted(
                            {(warning.code, warning.page_number) for warning in resolved_page_warnings},
                            key=lambda item: (item[1] or 0, item[0]),
                        )
                    ],
                },
            )
        )

    @staticmethod
    def _warning_fingerprint(warning: ParseWarning) -> str:
        """Return a canonical full-value fingerprint for warning mirrors."""

        return json.dumps(
            warning.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    async def parse(
        self,
        source: StoredSource | str | Path,
        options: DocumentParseOptions | dict[str, object],
        *,
        document_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult:
        stored = self._coerce_source(source)
        parsed_options = options if isinstance(options, DocumentParseOptions) else DocumentParseOptions.model_validate(options)
        try:
            parse_page_range(parsed_options.page_range, stored.page_count)
        except PageRangeError as exc:
            raise ServiceError("invalid_page_range", str(exc), status_code=400) from exc
        timeout = float(parsed_options.timeout_seconds or setting(self.settings, "document_timeout_seconds", 900))
        identifier = document_id or new_document_id()
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                await self._enter_parse()
                try:
                    if not self._initialized:
                        await self.initialize()
                    # Resolve after the reload gate: a waiter must never retain a
                    # parser instance that reload has just closed and replaced.
                    parser = self.registry.get(parsed_options.mode)
                    async with self._semaphore:
                        result = await parser.parse(
                            stored,
                            parsed_options,
                            document_id=identifier,
                            progress_callback=progress_callback,
                            cancel_event=cancel_event,
                        )
                        if parsed_options.mode == ParseMode.AUTO:
                            suspicious_pages = self.quality_service.suspicious_unicode_pages(result)
                            if suspicious_pages:
                                await self._repair_auto_mojibake(
                                    result,
                                    stored,
                                    parsed_options,
                                    suspicious_pages,
                                    document_id=identifier,
                                    cancel_event=cancel_event,
                                    progress_callback=progress_callback,
                                )
                            await self._normalize_auto_text(result, stored)
                            self._normalize_auto_short_mapped_tokens(result)
                        self._finalize_exports(result, parsed_options)
                        assessment = self.quality_service.assess(result)
                        if (
                            parsed_options.mode == ParseMode.AUTO
                            and parsed_options.enable_vlm_fallback
                            and not assessment.acceptable
                            and assessment.fallback_pages
                        ):
                            await self._apply_vlm_fallback(
                                result,
                                stored,
                                parsed_options,
                                assessment.fallback_pages,
                                document_id=identifier,
                                cancel_event=cancel_event,
                            )
                            self._finalize_exports(result, parsed_options)
                            self.quality_service.assess(result)
                        if parsed_options.mode == ParseMode.AUTO:
                            try:
                                await self._enrich_auto_diagrams(
                                    result,
                                    stored,
                                    parsed_options,
                                    cancel_event=cancel_event,
                                    progress_callback=progress_callback,
                                )
                            except (ParserCancelledError, asyncio.CancelledError):
                                raise
                            except Exception as exc:
                                result.warnings.append(
                                    ParseWarning(
                                        code="diagram_enrichment_failed",
                                        message="diagram enrichment failed unexpectedly; the source placeholder was retained",
                                        backend=self.vlm_parser.name,
                                        details={
                                            "reason": type(exc).__name__,
                                            "source_retained": "markdown_image_placeholder",
                                        },
                                    )
                                )
                            self._finalize_exports(result, parsed_options)
                        result.usage.duration_ms = max(0, round((time.perf_counter() - started) * 1000))
                        return result
                finally:
                    await self._leave_parse()
        except TimeoutError as exc:
            raise ServiceError("parse_timeout", f"document parsing exceeded {timeout:g} seconds", status_code=504) from exc
        except ParserCancelledError as exc:
            raise ServiceError("job_cancelled", str(exc), status_code=409) from exc
        except ParserUnavailableError as exc:
            details: dict[str, object] = {"mode": parsed_options.mode.value}
            if exc.details:
                details.update(exc.details)
            raise ServiceError(
                "backend_unavailable",
                str(exc),
                status_code=502,
                details=details,
            ) from exc
        except ParserError as exc:
            raise ServiceError(exc.code, str(exc), status_code=502, details=exc.details) from exc
