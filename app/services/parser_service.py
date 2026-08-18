"""Parser coordination, timeout handling, normalization, and conservative fallback."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from app.models.backend import BackendStatus
from app.models.error import ServiceError
from app.models.parse_options import ContentParseOptions, VlmPolicy
from app.models.parse_result import (
    DocumentParseResult,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    PageStatus,
    ParseWarning,
    SelectionStrategy,
    WarningSeverity,
)
from app.models.source import StoredSource
from app.parsers import (
    DoclingStandardParser,
    DocumentParser,
    GlmOcrRemoteAdapter,
    GlmSdkRemoteParser,
    OllamaVisualAdapter,
    ParserCancelledError,
    ParserError,
    ParserUnavailableError,
    ProgressCallback,
)
from app.security.file_validation import (
    DOCX_MIME,
    IMAGE_MIME_TYPES,
    PPTX_MIME,
    VIDEO_MIME_TYPES,
    FileValidationError,
    validate_stored_file,
)
from app.services.evidence_service import (
    NativeBlock,
    PageEvidence,
    PageEvidenceService,
    compact_text,
    date_tokens,
    multiset_coverage,
    number_tokens,
    reading_order_inverted,
    safe_cjk_compatibility_map,
)
from app.services.export_service import ExportService
from app.services.ir_service import DocumentIRService
from app.services.multimodal_service import MultimodalService
from app.services.quality_service import QualityService
from app.services.storage_service import StorageService
from app.services.visual_fusion_service import VisualFusionService
from app.utils.ids import new_content_id
from app.utils.page_range import PageRangeError, format_page_range, parse_page_range
from app.utils.settings import setting

logger = logging.getLogger(__name__)


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
        quality_service: QualityService | None = None,
        export_service: ExportService | None = None,
    ) -> None:
        self.settings = settings
        self.quality_service = quality_service or QualityService(settings)
        self.export_service = export_service or ExportService()
        self.evidence_service = PageEvidenceService(settings)
        self.ir_service = DocumentIRService(settings)
        self._semaphore = asyncio.Semaphore(int(setting(settings, "parser_workers", 1)))
        self._lifecycle_condition = asyncio.Condition()
        self._reloading = False
        self._active_parses = 0
        self._initialized = False
        self._build_adapters()

    def _build_adapters(self) -> None:
        self.glm_adapter = GlmOcrRemoteAdapter(self.settings)
        self.glm_sdk_parser = GlmSdkRemoteParser(self.settings)
        self.standard_parser = DoclingStandardParser(self.settings, self.glm_adapter)
        self.vlm_parser = OllamaVisualAdapter(self.settings)
        self.visual_fusion_service = VisualFusionService(self.settings, self.vlm_parser)
        self.multimodal_service = MultimodalService(
            self.settings, StorageService(self.settings), self.vlm_parser
        )

    async def initialize(self) -> None:
        if self._initialized:
            return
        await asyncio.gather(
            self.standard_parser.initialize(),
            self.glm_sdk_parser.initialize(),
            self.vlm_parser.initialize(),
        )
        # A missing optional backend must not prevent CPU/native-text startup.
        self._initialized = True

    async def reload(self) -> None:
        await self._begin_exclusive_lifecycle()
        try:
            await self._close_adapters()
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
            self.glm_sdk_parser.close(),
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
        docling, glm, glm_sdk, vlm = await asyncio.gather(
            self.standard_parser.probe(),
            self.glm_adapter.probe(),
            self.glm_sdk_parser.probe(),
            self.vlm_parser.probe(),
        )
        return [docling, glm, glm_sdk, vlm]

    def _coerce_source(self, source: StoredSource | str | Path) -> StoredSource:
        if isinstance(source, StoredSource):
            return source
        path = Path(source)
        try:
            filename, mime_type, size, page_count = validate_stored_file(
                path,
                path.name,
                max_bytes=int(setting(self.settings, "max_upload_bytes", 200 * 1024 * 1024)),
                max_pages=int(setting(self.settings, "max_content_units", 1_000)),
                max_video_seconds=int(setting(self.settings, "max_video_seconds", 30 * 60)),
                max_video_frame_pixels=int(
                    setting(self.settings, "video_max_frame_pixels", 7680 * 4320)
                ),
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

    def _finalize_exports(self, result: DocumentParseResult, options: ContentParseOptions) -> None:
        for page in result.pages:
            page.plain_text = self.export_service.markdown_to_text(page.content or "")
        markdown, plain_text = self.export_service.join_document(
            result.pages,
            preserve_page_breaks=True,
            table_fragments=result._table_fragments,
            merge_cross_page_tables=bool(
                setting(self.settings, "cross_page_table_merge_enabled", True)
            ),
        )
        result.markdown = markdown
        result.plain_text = plain_text
        result.processed_pages = sum(page.status.value != "failed" for page in result.pages)

    async def _page_evidence(
        self,
        source: StoredSource,
        result: DocumentParseResult,
    ) -> dict[int, PageEvidence]:
        pages = {page.page_number for page in result.pages}
        evidence = await asyncio.to_thread(self.evidence_service.inspect, source, pages)
        for page in result.pages:
            item = evidence.get(page.page_number)
            if item is None:
                continue
            page.diagnostics = PageDiagnostics(
                source_kind=item.source_kind,
                native_text_characters=item.native_text_characters,
                visual_ink_ratio=item.visual_ink_ratio,
                image_coverage_ratio=item.image_coverage_ratio,
                detected_rotation_degrees=item.detected_rotation_degrees,  # type: ignore[arg-type]
            )
        return evidence

    @staticmethod
    def _replace_evidence_glyphs(page: PageParseResult, evidence: PageEvidence) -> bool:
        content = page.content or ""
        replacements = dict(evidence.glyph_mappings)
        replacements.update(safe_cjk_compatibility_map(content, evidence.native_text))
        compact_native = compact_text(evidence.native_body_text)
        compact_content = compact_text(content)
        matcher = SequenceMatcher(None, compact_content, compact_native, autojunk=False)
        for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
            if operation != "replace" or left_end - left_start != 1 or right_end - right_start != 1:
                continue
            source_character = compact_content[left_start:left_end]
            target_character = compact_native[right_start:right_end]
            if source_character and unicodedata.category(source_character) == "Co":
                replacements[source_character] = target_character
        changed = False
        for source_character, target_character in replacements.items():
            if source_character in content:
                content = content.replace(source_character, target_character)
                changed = True
        if changed:
            page.content = content
            page.plain_text = None
        return changed

    @staticmethod
    def _looks_like_table(content: str) -> bool:
        return (
            sum(
                line.strip().startswith("|") and line.count("|") >= 2
                for line in content.splitlines()
            )
            >= 2
        )

    @staticmethod
    def _refresh_table_fragment_renderings(result: DocumentParseResult) -> None:
        """Keep fragment byte references aligned after deterministic text normalization."""

        pages = {page.page_number: page for page in result.pages}
        for fragment in result._table_fragments:
            page_number = getattr(fragment, "page_number", None)
            if not isinstance(page_number, int):
                continue
            page = pages.get(page_number)
            fragment_id = str(getattr(fragment, "fragment_id", "") or "")
            content = page.content if page is not None else None
            if not content or not fragment_id:
                continue
            marker = f"<!-- table-fragment: {fragment_id} -->"
            pattern = re.compile(
                rf"{re.escape(marker)}\n\n(?P<table>(?:\|[^\n]*(?:\n|$))+)",
            )
            match = pattern.search(content)
            if match is None:
                continue
            markdown = match.group("table").rstrip()
            fragment.markdown = markdown  # type: ignore[attr-defined]
            fragment.rendered = f"{marker}\n\n{markdown}"  # type: ignore[attr-defined]

    @staticmethod
    def _native_blocks_outside_tables(
        evidence: PageEvidence,
        fragments: list[object],
    ) -> tuple[list[NativeBlock], list[tuple[float, float, float, float]]]:
        table_regions: list[tuple[float, float, float, float]] = []
        if evidence.page_width <= 0 or evidence.page_height <= 0:
            return list(evidence.native_blocks), table_regions
        for fragment in fragments:
            normalized = getattr(fragment, "normalized_bbox", None)
            if not normalized:
                continue
            left, top, right, bottom = normalized
            table_regions.append(
                (
                    left * evidence.page_width,
                    top * evidence.page_height,
                    right * evidence.page_width,
                    bottom * evidence.page_height,
                )
            )

        def overlaps_table(block: NativeBlock) -> bool:
            block_area = max(
                1.0,
                (block.bbox[2] - block.bbox[0]) * (block.bbox[3] - block.bbox[1]),
            )
            for region in table_regions:
                intersection = max(
                    0.0,
                    min(block.bbox[2], region[2]) - max(block.bbox[0], region[0]),
                ) * max(
                    0.0,
                    min(block.bbox[3], region[3]) - max(block.bbox[1], region[1]),
                )
                if intersection / block_area >= 0.25:
                    return True
            return False

        return (
            [block for block in evidence.native_blocks if not overlaps_table(block)],
            table_regions,
        )

    @classmethod
    def _native_repair_candidate(
        cls,
        content: str,
        evidence: PageEvidence,
        fragments: list[object],
    ) -> str:
        """Reorder existing Markdown nodes using native geometry without inventing text."""

        body_blocks, _table_regions = cls._native_blocks_outside_tables(evidence, fragments)
        if not body_blocks:
            return ""
        working = content
        table_nodes: dict[str, tuple[str, NativeBlock]] = {}
        for index, fragment in enumerate(fragments):
            normalized = getattr(fragment, "normalized_bbox", None)
            rendered = str(getattr(fragment, "rendered", "") or "")
            if (
                not normalized
                or not rendered
                or evidence.page_width <= 0
                or evidence.page_height <= 0
                or rendered not in working
            ):
                return ""
            left, top, right, bottom = normalized
            marker = f"\x00table-{index}\x00"
            block = NativeBlock(
                text=marker,
                bbox=(
                    left * evidence.page_width,
                    top * evidence.page_height,
                    right * evidence.page_width,
                    bottom * evidence.page_height,
                ),
                max_font_size=0,
                bold=False,
            )
            table_nodes[marker] = (rendered, block)
            working = working.replace(rendered, marker, 1)

        rendered_nodes: dict[str, str] = {
            marker: rendered for marker, (rendered, _block) in table_nodes.items()
        }
        layout_blocks = [block for _rendered, block in table_nodes.values()]
        body_order = {id(block): index for index, block in enumerate(body_blocks)}
        claimed_blocks: set[int] = set()
        text_index = 0

        def normalized_block_text(block: NativeBlock) -> str:
            value = block.text
            for source_character, target_character in evidence.glyph_mappings.items():
                value = value.replace(source_character, target_character)
            return compact_text(value)

        for part in re.split(r"\n\s*\n", working):
            stripped = part.strip()
            if not stripped:
                continue
            if stripped in table_nodes:
                continue
            node_text = compact_text(stripped)
            if not node_text:
                return ""
            matches = [
                block
                for block in body_blocks
                if (block_text := normalized_block_text(block))
                and len(block_text) >= 4
                and (block_text in node_text or node_text in block_text)
                and id(block) not in claimed_blocks
            ]
            if not matches:
                return ""
            matches.sort(key=lambda block: body_order[id(block)])
            normalized_matches = {normalized_block_text(block) for block in matches}
            if len(matches) > 1 and len(normalized_matches) == 1:
                matches = matches[:1]
            matched_text = "".join(block.text for block in matches)
            for source_character, target_character in evidence.glyph_mappings.items():
                matched_text = matched_text.replace(source_character, target_character)
            if (
                multiset_coverage(stripped, matched_text) < 0.98
                or multiset_coverage(matched_text, stripped) < 0.98
            ):
                return ""
            claimed_blocks.update(id(block) for block in matches)
            marker = f"\x00text-{text_index}\x00"
            text_index += 1
            rendered_nodes[marker] = stripped
            layout_blocks.append(
                NativeBlock(
                    text=marker,
                    bbox=(
                        min(block.bbox[0] for block in matches),
                        min(block.bbox[1] for block in matches),
                        max(block.bbox[2] for block in matches),
                        max(block.bbox[3] for block in matches),
                    ),
                    max_font_size=max(block.max_font_size for block in matches),
                    bold=any(block.bold for block in matches),
                )
            )

        layout = PageEvidence(
            page_number=evidence.page_number,
            source_kind=evidence.source_kind,
            native_blocks=layout_blocks,
            page_width=evidence.page_width,
            page_height=evidence.page_height,
        )._blocks_in_reading_order()
        if len(layout) != len(rendered_nodes):
            return ""
        return "\n\n".join(rendered_nodes[block.text] for block in layout).strip()

    @staticmethod
    def _repair_split_headings(
        result: DocumentParseResult,
        evidence_by_page: dict[int, PageEvidence],
    ) -> None:
        pattern = re.compile(
            r"(?m)^(?P<level>#{1,6})[ \t]+(?P<left>[^\n]+)\n[ \t]*\n"
            r"(?P=level)[ \t]*(?P<right>[^\n]+)$"
        )
        repaired_pages: list[int] = []
        for page in result.pages:
            evidence = evidence_by_page.get(page.page_number)
            content = page.content or ""
            if evidence is None or evidence.source_kind != PageSourceKind.NATIVE:
                continue
            native_lines = Counter(compact_text(line) for line in evidence.native_lines)
            changed = False

            def merge(
                match: re.Match[str],
                native_line_counts: Counter[str] = native_lines,
            ) -> str:
                nonlocal changed
                left = match.group("left").rstrip()
                right = match.group("right").lstrip()
                if len(compact_text(left)) > 4 and not right.startswith(tuple("、，。：；）】》")):
                    return match.group(0)
                combined = f"{left}{right}"
                if native_line_counts[compact_text(combined)] != 1:
                    return match.group(0)
                changed = True
                return f"{match.group('level')} {combined}"

            repaired = pattern.sub(merge, content)
            if not changed:
                continue
            page.content = repaired
            page.plain_text = None
            if page.diagnostics is not None:
                page.diagnostics.selected_strategy = SelectionStrategy.NATIVE_REPAIR
            repaired_pages.append(page.page_number)
        if repaired_pages:
            result.warnings.append(
                ParseWarning(
                    code="split_heading_repaired",
                    message=f"native line evidence rejoined split headings on {len(repaired_pages)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend="pymupdf-native",
                    details={"pages": repaired_pages},
                )
            )

    @classmethod
    def _remove_unanchored_table_numbers(
        cls,
        result: DocumentParseResult,
        evidence_by_page: dict[int, PageEvidence],
    ) -> None:
        standalone = re.compile(r"^[ \t]*(?P<value>[-−]?\(?\d[\d,]*\.\d+%?\)?)[ \t]*$")
        repaired_pages: list[int] = []
        for page in result.pages:
            evidence = evidence_by_page.get(page.page_number)
            fragments = [
                fragment
                for fragment in result._table_fragments
                if getattr(fragment, "page_number", None) == page.page_number
            ]
            if evidence is None or not fragments:
                continue
            _body_blocks, regions = cls._native_blocks_outside_tables(evidence, fragments)
            table_numbers = Counter(
                number
                for fragment in fragments
                for number in number_tokens(str(getattr(fragment, "markdown", "") or ""))
            )

            def positioned_in_table(
                value: str,
                native_blocks: list[NativeBlock] = evidence.native_blocks,
                table_regions: list[tuple[float, float, float, float]] = regions,
            ) -> bool:
                for block in native_blocks:
                    if value not in number_tokens(block.text):
                        continue
                    center_x = (block.bbox[0] + block.bbox[2]) / 2
                    center_y = (block.bbox[1] + block.bbox[3]) / 2
                    if any(
                        left <= center_x <= right and top <= center_y <= bottom
                        for left, top, right, bottom in table_regions
                    ):
                        return True
                return False

            removed = 0
            retained_lines: list[str] = []
            for line in (page.content or "").splitlines():
                match = standalone.fullmatch(line)
                values = number_tokens(match.group("value")) if match else []
                value = values[0] if len(values) == 1 else ""
                if value and table_numbers[value] and positioned_in_table(value):
                    removed += 1
                    continue
                retained_lines.append(line)
            if not removed:
                continue
            page.content = re.sub(r"\n{3,}", "\n\n", "\n".join(retained_lines)).strip()
            page.plain_text = None
            if page.diagnostics is not None:
                page.diagnostics.selected_strategy = SelectionStrategy.NATIVE_REPAIR
            repaired_pages.append(page.page_number)
        if repaired_pages:
            result.warnings.append(
                ParseWarning(
                    code="unanchored_table_numbers_removed",
                    message=f"position and table evidence removed duplicated standalone numbers on {len(repaired_pages)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend="pymupdf-native",
                    details={"pages": repaired_pages},
                )
            )

    def _repair_native_reading_order(
        self,
        result: DocumentParseResult,
        evidence_by_page: dict[int, PageEvidence],
    ) -> None:
        repair_enabled = bool(setting(self.settings, "native_text_repair_enabled", True))
        minimum_anchors = int(setting(self.settings, "quality_reading_order_min_anchors", 4))
        minimum_coverage = float(setting(self.settings, "quality_native_repair_min_coverage", 0.98))
        minimum_ratio = float(
            setting(self.settings, "quality_native_repair_min_length_ratio", 0.95)
        )
        maximum_ratio = float(
            setting(self.settings, "quality_native_repair_max_length_ratio", 1.05)
        )
        repaired_pages: list[int] = []
        for page in result.pages:
            evidence = evidence_by_page.get(page.page_number)
            content = page.content or ""
            page_fragments = [
                fragment
                for fragment in result._table_fragments
                if getattr(fragment, "page_number", None) == page.page_number
            ]
            body_blocks, _table_regions = (
                self._native_blocks_outside_tables(evidence, page_fragments)
                if evidence is not None
                else ([], [])
            )
            if (
                evidence is None
                or evidence.source_kind != PageSourceKind.NATIVE
                or not reading_order_inverted(
                    evidence,
                    content,
                    minimum_anchors,
                    blocks=body_blocks,
                )
            ):
                continue
            if not repair_enabled:
                page.warnings.append(
                    ParseWarning(
                        code="reading_order_inversion",
                        message="native visual-order anchors occur out of order in parsed output",
                        page_number=page.page_number,
                        backend=page.backend,
                        details={"repair_disabled": True},
                    )
                )
                continue
            candidate = self._native_repair_candidate(content, evidence, page_fragments)
            reference_length = max(1, len(compact_text(content)))
            candidate_ratio = len(compact_text(candidate)) / reference_length
            rejection_codes: list[str] = []
            if (
                multiset_coverage(candidate, content) < minimum_coverage
                or multiset_coverage(content, candidate) < minimum_coverage
            ):
                rejection_codes.append("character_coverage")
            if not minimum_ratio <= candidate_ratio <= maximum_ratio:
                rejection_codes.append("length_ratio")
            if Counter(number_tokens(candidate)) != Counter(number_tokens(content)):
                rejection_codes.append("numeric_mismatch")
            if date_tokens(candidate) != date_tokens(content):
                rejection_codes.append("date_mismatch")
            if Counter(compact_text(candidate)) != Counter(compact_text(content)):
                rejection_codes.append("non_unique_block_match")
            diagnostics = page.diagnostics
            if rejection_codes:
                page.warnings.append(
                    ParseWarning(
                        code="reading_order_inversion",
                        message="native visual-order anchors occur out of order in parsed output",
                        page_number=page.page_number,
                        backend=page.backend,
                        details={"repair_rejected": rejection_codes},
                    )
                )
                continue
            page.content = candidate
            page.plain_text = None
            if diagnostics is not None:
                diagnostics.selected_strategy = SelectionStrategy.NATIVE_REPAIR
            repaired_pages.append(page.page_number)
        if repaired_pages:
            result.warnings.append(
                ParseWarning(
                    code="native_reading_order_repaired",
                    message=f"native PDF geometry repaired reading order on {len(repaired_pages)} page(s)",
                    severity=WarningSeverity.INFO,
                    backend="pymupdf-native",
                    details={"pages": repaired_pages},
                )
            )

    @staticmethod
    def _normalize_directory_pages(
        result: DocumentParseResult,
        evidence_by_page: dict[int, PageEvidence],
    ) -> None:
        entry = re.compile(r"^(?P<title>.+?)[.·…]{4,}\s*(?P<page>\d+)\s*$")
        normalized_pages: set[int] = set()
        for page in result.pages:
            evidence = evidence_by_page.get(page.page_number)
            if evidence is None or not any(
                line.strip() == "目录" for line in evidence.native_lines
            ):
                continue
            entries = [
                match for line in evidence.native_lines if (match := entry.match(line.strip()))
            ]
            if len(entries) < 3:
                continue
            page.content = "# 目录\n\n" + "\n".join(
                f"- {match.group('title').rstrip('.·… ')} …… {match.group('page')}"
                for match in entries
            )
            page.plain_text = None
            if page.diagnostics is not None:
                page.diagnostics.selected_strategy = SelectionStrategy.NATIVE_REPAIR
            normalized_pages.add(page.page_number)
        if normalized_pages:
            result._table_fragments = [
                fragment
                for fragment in result._table_fragments
                if getattr(fragment, "page_number", None) not in normalized_pages
            ]

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
                        evidence.words.update(
                            match.group().casefold() for match in cls._NATIVE_WORD.finditer(token)
                        )
                        evidence.compounds.update(
                            match.group().casefold()
                            for match in cls._NATIVE_COMPOUND.finditer(token)
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
                        prefix = "".join(
                            cls._CONTEXT_CHARACTER.findall(native_text[: match.start()])
                        )[-6:]
                        suffix = "".join(
                            cls._CONTEXT_CHARACTER.findall(native_text[match.end() :])
                        )[:6]
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
            if character in {"\n", "\t"}:
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
            output.append(character)
        cleaned = "".join(output)
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
                if match.string[match.start() - 1] + match.string[match.end()]
                in native_cjk_space_boundaries
                else ""
            ),
            text,
        )
        text = cls._SPACE_BEFORE_CJK_PUNCT.sub("", text)
        text = cls._SPACE_AFTER_CJK_PUNCT.sub("", text)
        text = cls._ALNUM_RUN.sub(
            lambda match: cls._merge_native_fragments(match, native_words), text
        )
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
                    suspicious = dict(glyphs)
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
                    visible_characters = max(
                        1, sum(not character.isspace() for character in original)
                    )
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
                        details={
                            "detected_codepoints": page_suspicious,
                            "characters_preserved": True,
                        },
                    )
                )
        if changed_pages or suspicious_glyph_pages:
            details: dict[str, object] = {
                "pages": sorted(changed_pages),
                "suspicious_glyph_pages": sorted(suspicious_glyph_pages),
                "detected_codepoints": sanitized,
                "mapped_codepoints": {},
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
                lambda match: (
                    f"{match.group('prefix')}{cls._BROKEN_APPENDIX_LETTERS[match.group('glyph')]}"
                ),
                normalized,
            )
            return cls._BROKEN_APPENDIX_HEADING.sub(
                lambda match: (
                    f"{match.group('prefix')}{cls._BROKEN_APPENDIX_LETTERS[match.group('glyph')]}"
                ),
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

    def _complex_visual_targets(
        self,
        result: DocumentParseResult,
        evidence_by_page: dict[int, PageEvidence],
    ) -> dict[int, str]:
        """Select only measured scanned/mixed tables and signature-heavy visual pages."""

        if not bool(setting(self.settings, "visual_router_enabled", True)):
            return {}
        minimum_area = float(setting(self.settings, "quality_table_min_grid_area_ratio", 0.25))
        table_failure_codes = {
            "low_text_content",
            "repeated_text",
            "table_header_propagation",
            "table_shape_explosion",
            "table_structure_invalid",
            "unanchored_table_numbers",
            "visual_text_mismatch",
        }
        signature_hint = re.compile(
            r"注册会计师|法定代表人|主管会计工作负责人|会计机构负责人|签名|签字|盖章"
        )
        targets: dict[int, str] = {}
        for page in result.pages:
            evidence = evidence_by_page.get(page.page_number)
            if evidence is None or evidence.source_kind not in {
                PageSourceKind.SCANNED,
                PageSourceKind.MIXED,
            }:
                continue
            warning_codes = {
                warning.code
                for warning in page.warnings
                if warning.severity != WarningSeverity.INFO
            }
            complex_grid = (
                evidence.has_complex_grid
                and evidence.grid_area_ratio >= minimum_area
                and evidence.horizontal_grid_lines >= 3
                and evidence.vertical_grid_lines >= 3
            )
            table_failure = bool(warning_codes & table_failure_codes) and bool(
                evidence.grid_regions
            )
            if complex_grid or table_failure:
                targets[page.page_number] = "table"
                continue
            content = page.content or ""
            signature_page = bool(signature_hint.search(content)) and (
                "<!-- image -->" in content
                or (evidence.image_coverage_ratio or 0.0) >= 0.20
                or "repeated_text" in warning_codes
            )
            if signature_page:
                targets[page.page_number] = "signature"
        return targets

    async def _apply_visual_fusion(
        self,
        result: DocumentParseResult,
        source: StoredSource,
        options: ContentParseOptions,
        evidence_by_page: dict[int, PageEvidence],
        eligible_pages: set[int] | None = None,
        *,
        document_id: str,
        cancel_event: object | None,
    ) -> None:
        """Fuse GLM layout/OCR and regional Qwen reasoning without replacing whole pages."""

        targets = self._complex_visual_targets(result, evidence_by_page)
        if eligible_pages is not None:
            targets = {
                page_number: target_kind
                for page_number, target_kind in targets.items()
                if page_number in eligible_pages
            }
        if not targets:
            return
        if getattr(self.vlm_parser, "enabled", None) is False:
            result.warnings.append(
                ParseWarning(
                    code="visual_fusion_unavailable",
                    message="complex visual pages were retained because Qwen is disabled",
                    backend=self.vlm_parser.name,
                    details={"pages": sorted(targets)},
                )
            )
            return
        qwen_status = await self.vlm_parser.probe()
        if not qwen_status.ready:
            result.warnings.append(
                ParseWarning(
                    code="visual_fusion_unavailable",
                    message="complex visual pages were retained because Qwen is unavailable",
                    backend=self.vlm_parser.name,
                    details={"pages": sorted(targets)},
                )
            )
            return

        glm_result: DocumentParseResult | None = None
        if bool(setting(self.settings, "glm_sdk_enabled", False)):
            glm_status = await self.glm_sdk_parser.probe()
            if glm_status.ready:
                sdk_options = options.model_copy(
                    update={"unit_range": format_page_range(sorted(targets))}
                )
                try:
                    glm_result = await self.glm_sdk_parser.parse(
                        source,
                        sdk_options,
                        document_id=document_id,
                        cancel_event=cancel_event,
                    )
                except (ParserCancelledError, asyncio.CancelledError):
                    raise
                except ParserError:
                    glm_result = None

        primary_by_page = {page.page_number: page for page in result.pages}
        glm_by_page = (
            {page.page_number: page for page in glm_result.pages} if glm_result is not None else {}
        )
        glm_fragments_by_page: dict[int, list[object]] = {}
        if glm_result is not None:
            for fragment in glm_result._table_fragments:
                page_number = getattr(fragment, "page_number", None)
                if isinstance(page_number, int):
                    glm_fragments_by_page.setdefault(page_number, []).append(fragment)
        primary_fragments_by_page: dict[int, list[object]] = {}
        for fragment in result._table_fragments:
            page_number = getattr(fragment, "page_number", None)
            if isinstance(page_number, int):
                primary_fragments_by_page.setdefault(page_number, []).append(fragment)

        adopted: dict[int, PageParseResult] = {}
        visual_page_irs: dict[int, object] = {}
        replacement_fragments: list[object] = []
        for page_number, target_kind in sorted(targets.items()):
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                raise ParserCancelledError("job was cancelled")
            primary = primary_by_page[page_number]
            glm_page = glm_by_page.get(page_number)
            glm_fragments = [
                fragment
                for fragment in glm_fragments_by_page.get(page_number, [])
                if hasattr(fragment, "rows")
            ]
            docling_fragments = [
                fragment
                for fragment in primary_fragments_by_page.get(page_number, [])
                if hasattr(fragment, "rows")
            ]
            try:
                if target_kind == "table":
                    outcome = await self.visual_fusion_service.fuse_table_page(
                        source,
                        options,
                        primary,
                        evidence_by_page[page_number],
                        glm_page,
                        glm_fragments,  # type: ignore[arg-type]
                        docling_fragments,  # type: ignore[arg-type]
                    )
                else:
                    outcome = await self.visual_fusion_service.fuse_signature_page(
                        source,
                        options,
                        primary,
                        evidence_by_page[page_number],
                        glm_page,
                    )
            except (ParserCancelledError, asyncio.CancelledError):
                raise
            except Exception as exc:
                primary.status = PageStatus.WARNING
                primary.warnings.append(
                    ParseWarning(
                        code="visual_fusion_partial",
                        message="regional visual fusion failed and retained the primary page",
                        page_number=page_number,
                        backend=self.vlm_parser.name,
                        details={"reason": type(exc).__name__, "target_kind": target_kind},
                    )
                )
                continue
            adopted[page_number] = outcome.page
            visual_page_irs[page_number] = outcome.ir
            replacement_fragments.extend(outcome.fragments)

        if not adopted:
            return
        result.pages = [adopted.get(page.page_number, page) for page in result.pages]
        result._table_fragments = [
            fragment
            for fragment in result._table_fragments
            if getattr(fragment, "page_number", None) not in adopted
        ]
        result._table_fragments.extend(replacement_fragments)
        result._visual_page_irs.update(visual_page_irs)
        result.warnings = [
            warning
            for warning in result.warnings
            if not (
                warning.page_number in adopted
                and warning.code in self.visual_fusion_service._RESOLVED_WARNING_CODES
            )
        ]
        result.pipeline.primary = "visual-fusion"
        result.pipeline.ocr = (
            self.glm_sdk_parser.name if glm_result is not None else result.pipeline.ocr
        )
        result.pipeline.vlm = self.vlm_parser.name
        result._vlm_page_numbers.update(adopted)
        result.route_summary.vlm_pages = len(result._vlm_page_numbers)
        result.warnings.append(
            ParseWarning(
                code="qwen_visual_fusion_used",
                message=f"Qwen regional visual fusion assembled {len(adopted)} page(s)",
                severity=WarningSeverity.INFO,
                backend=self.vlm_parser.name,
                details={"pages": sorted(adopted)},
            )
        )

    async def parse(
        self,
        source: StoredSource | str | Path,
        options: ContentParseOptions | dict[str, object],
        *,
        document_id: str | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult:
        stored = self._coerce_source(source)
        parsed_options = (
            options
            if isinstance(options, ContentParseOptions)
            else ContentParseOptions.model_validate(options)
        )
        if stored.mime_type in VIDEO_MIME_TYPES and parsed_options.unit_range is not None:
            raise ServiceError(
                "invalid_unit_range", "video inputs do not accept unit_range", status_code=400
            )
        if (
            stored.mime_type == DOCX_MIME
            or (stored.mime_type in IMAGE_MIME_TYPES and stored.mime_type != "image/tiff")
        ) and parsed_options.unit_range not in {None, "1"}:
            raise ServiceError(
                "invalid_unit_range",
                "DOCX and single-frame images only accept an omitted unit_range or 1",
                status_code=400,
            )
        try:
            parse_page_range(parsed_options.page_range, stored.page_count)
        except PageRangeError as exc:
            raise ServiceError("invalid_unit_range", str(exc), status_code=400) from exc
        timeout = float(
            parsed_options.timeout_seconds or setting(self.settings, "content_timeout_seconds", 900)
        )
        identifier = document_id or new_content_id()
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                await self._enter_parse()
                try:
                    if not self._initialized:
                        await self.initialize()
                    if stored.mime_type in VIDEO_MIME_TYPES:
                        return await self.multimodal_service.parse_video(
                            stored,
                            parsed_options,
                            content_id=identifier,
                            progress_callback=progress_callback,
                            cancel_event=cancel_event,
                        )
                    if stored.mime_type in {DOCX_MIME, PPTX_MIME}:
                        async with self._semaphore:
                            return await self.multimodal_service.parse_office(
                                stored,
                                parsed_options,
                                content_id=identifier,
                                progress_callback=progress_callback,
                                cancel_event=cancel_event,
                                image_parser=self.standard_parser,
                            )
                    # Resolve after the reload gate: a waiter must never retain a
                    # parser instance that reload has just closed and replaced.
                    execution_options = parsed_options
                    parser: DocumentParser = self.standard_parser
                    async with self._semaphore:
                        result = await parser.parse(
                            stored,
                            execution_options,
                            document_id=identifier,
                            progress_callback=progress_callback,
                            cancel_event=cancel_event,
                        )
                        evidence_by_page = await self._page_evidence(stored, result)
                        await self._normalize_auto_text(result, stored)
                        self._normalize_auto_short_mapped_tokens(result)
                        self._refresh_table_fragment_renderings(result)
                        glyph_repaired_pages = [
                            page.page_number
                            for page in result.pages
                            if (evidence := evidence_by_page.get(page.page_number)) is not None
                            and self._replace_evidence_glyphs(page, evidence)
                        ]
                        for page in result.pages:
                            if (
                                page.page_number in glyph_repaired_pages
                                and not self._suspicious_glyph_counts(page.content or "")
                            ):
                                page.warnings = [
                                    warning
                                    for warning in page.warnings
                                    if warning.code != "suspicious_unicode_glyphs"
                                ]
                        if glyph_repaired_pages:
                            result.warnings.append(
                                ParseWarning(
                                    code="native_glyph_repaired",
                                    message=f"font and position evidence repaired glyphs on {len(glyph_repaired_pages)} page(s)",
                                    severity=WarningSeverity.INFO,
                                    backend="pymupdf-native",
                                    details={"pages": glyph_repaired_pages},
                                )
                            )
                        for page_number in self.quality_service.suspicious_unicode_pages(result):
                            page = next(
                                item for item in result.pages if item.page_number == page_number
                            )
                            page.status = PageStatus.WARNING
                            if not any(
                                warning.code == "suspicious_unicode_mojibake"
                                for warning in page.warnings
                            ):
                                page.warnings.append(
                                    ParseWarning(
                                        code="suspicious_unicode_mojibake",
                                        message="native font and position evidence could not safely repair a broken ToUnicode mapping",
                                        page_number=page.page_number,
                                        backend=page.backend,
                                    )
                                )
                        self._remove_unanchored_table_numbers(result, evidence_by_page)
                        self._repair_split_headings(result, evidence_by_page)
                        self._repair_native_reading_order(result, evidence_by_page)
                        self._normalize_directory_pages(result, evidence_by_page)
                        self._finalize_exports(result, parsed_options)
                        self.quality_service.assess(result)
                        visual_requested = (
                            parsed_options.profile.value == "accurate"
                            and parsed_options.resolved_vlm_policy == VlmPolicy.AUTO_VISUAL
                        )
                        if visual_requested:
                            await self._apply_visual_fusion(
                                result,
                                stored,
                                execution_options,
                                evidence_by_page,
                                document_id=identifier,
                                cancel_event=cancel_event,
                            )
                            self._finalize_exports(result, parsed_options)
                            self.quality_service.assess(result)
                        result.usage.duration_ms = max(
                            0, round((time.perf_counter() - started) * 1000)
                        )
                        result.evidence_ir = self.ir_service.build(
                            result,
                            stored,
                            evidence_by_page,
                        )
                        await self.multimodal_service.enrich_page_content(
                            result.evidence_ir,
                            result,
                            stored,
                            evidence_by_page,
                            parsed_options,
                            image_parser=self.standard_parser,
                            cancel_event=cancel_event,
                        )
                        result.markdown = result.evidence_ir.renderings.markdown
                        result.plain_text = result.evidence_ir.renderings.plain_text
                        if not parsed_options.include_renderings:
                            result.evidence_ir.renderings.markdown = ""
                            result.evidence_ir.renderings.plain_text = ""
                            for page_ir in result.evidence_ir.units:
                                page_ir.renderings.markdown = ""
                                page_ir.renderings.plain_text = ""
                            result.markdown = ""
                            result.plain_text = ""
                        return result
                finally:
                    await self._leave_parse()
        except TimeoutError as exc:
            raise ServiceError(
                "parse_timeout", f"content parsing exceeded {timeout:g} seconds", status_code=504
            ) from exc
        except ParserCancelledError as exc:
            raise ServiceError("job_cancelled", str(exc), status_code=409) from exc
        except ParserUnavailableError as exc:
            details: dict[str, object] = {"profile": parsed_options.profile.value}
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
