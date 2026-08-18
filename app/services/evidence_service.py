"""Measured PDF-page evidence used by quality checks and deterministic repair."""

from __future__ import annotations

import io
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.models.parse_result import PageSourceKind
from app.models.source import StoredSource
from app.utils.settings import setting

_COMPACT = re.compile(r"\s+")
_MARKDOWN = re.compile(r"(?:<!--.*?-->|[`#*_~>|\[\](){}-])", re.DOTALL)
_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-−]?\(?\d[\d,]*(?:\.\d+)?%?\)?")
_DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")


@dataclass(frozen=True, slots=True)
class NativeBlock:
    text: str
    bbox: tuple[float, float, float, float]
    max_font_size: float
    bold: bool


@dataclass(slots=True)
class PageEvidence:
    page_number: int
    source_kind: PageSourceKind
    native_text: str = ""
    native_body_text: str = ""
    native_lines: list[str] = field(default_factory=list)
    native_blocks: list[NativeBlock] = field(default_factory=list)
    native_text_characters: int | None = None
    image_coverage_ratio: float | None = None
    visual_ink_ratio: float | None = None
    detected_rotation_degrees: int | None = None
    glyph_mappings: dict[str, str] = field(default_factory=dict)
    horizontal_grid_lines: int = 0
    vertical_grid_lines: int = 0
    grid_area_ratio: float = 0.0
    page_width: float = 0.0
    page_height: float = 0.0
    grid_regions: list[tuple[float, float, float, float]] = field(default_factory=list)

    @property
    def has_complex_grid(self) -> bool:
        return self.horizontal_grid_lines >= 3 and self.vertical_grid_lines >= 3

    def native_markdown(self) -> str:
        if not self.native_blocks:
            return self.native_body_text.strip()
        blocks = self._blocks_in_reading_order()
        sizes = [block.max_font_size for block in blocks if block.max_font_size > 0]
        median_size = statistics.median(sizes) if sizes else 0.0
        rendered: list[str] = []
        for block in blocks:
            text = block.text.strip()
            if not text:
                continue
            compact = compact_text(text)
            heading = (
                len(compact) <= 80
                and median_size > 0
                and (block.max_font_size >= median_size * 1.28 or block.bold)
            )
            rendered.append(f"## {text}" if heading else text)
        return "\n\n".join(rendered).strip()

    def _blocks_in_reading_order(self) -> list[NativeBlock]:
        """Use visual columns only when geometry has a clear two-column split."""

        by_y = sorted(self.native_blocks, key=lambda block: (block.bbox[1], block.bbox[0]))
        if self.page_width <= 0:
            return by_y
        narrow = [
            block for block in by_y if block.bbox[2] - block.bbox[0] <= self.page_width * 0.68
        ]
        centers = sorted(
            (((block.bbox[0] + block.bbox[2]) / 2, block) for block in narrow),
            key=lambda item: (item[0], item[1].bbox[1]),
        )
        if len(centers) < 4:
            return by_y
        gaps = [
            (right[0] - left[0], index)
            for index, (left, right) in enumerate(zip(centers, centers[1:], strict=False))
        ]
        gap, index = max(gaps, default=(0.0, 0))
        left = [item[1] for item in centers[: index + 1]]
        right = [item[1] for item in centers[index + 1 :]]
        if gap < self.page_width * 0.18 or len(left) < 2 or len(right) < 2:
            return by_y
        column_ids = {id(block) for block in [*left, *right]}
        spanning = [block for block in by_y if id(block) not in column_ids]
        column_top = min(block.bbox[1] for block in [*left, *right])
        column_bottom = max(block.bbox[3] for block in [*left, *right])
        before = [block for block in spanning if block.bbox[3] <= column_top]
        after = [block for block in spanning if block.bbox[1] >= column_bottom]
        ambiguous = [block for block in spanning if block not in before and block not in after]
        if ambiguous:
            return by_y
        return [
            *sorted(before, key=lambda block: (block.bbox[1], block.bbox[0])),
            *sorted(left, key=lambda block: (block.bbox[1], block.bbox[0])),
            *sorted(right, key=lambda block: (block.bbox[1], block.bbox[0])),
            *sorted(after, key=lambda block: (block.bbox[1], block.bbox[0])),
        ]


def compact_text(value: str) -> str:
    return _COMPACT.sub("", _MARKDOWN.sub("", value)).casefold()


def number_tokens(value: str) -> list[str]:
    return [
        re.sub(r"[\s,−]", lambda match: "-" if match.group() == "−" else "", item)
        for item in _NUMBER.findall(value)
    ]


def date_tokens(value: str) -> set[str]:
    return {re.sub(r"\s+", "", item) for item in _DATE.findall(value)}


def multiset_coverage(reference: str, candidate: str) -> float:
    expected = Counter(compact_text(reference))
    if not expected:
        return 1.0 if not compact_text(candidate) else 0.0
    actual = Counter(compact_text(candidate))
    return sum((expected & actual).values()) / sum(expected.values())


def reading_order_inverted(
    evidence: PageEvidence,
    content: str,
    minimum_anchors: int,
    *,
    blocks: list[NativeBlock] | None = None,
) -> bool:
    parsed = compact_text(content)

    def normalized_anchor(value: str) -> str:
        for source_character, target_character in evidence.glyph_mappings.items():
            value = value.replace(source_character, target_character)
        return compact_text(value)

    candidates = (
        [
            normalized_anchor(line)
            for block in blocks
            for line in block.text.splitlines()
            if line.strip()
        ]
        if blocks is not None
        else [normalized_anchor(line) for line in evidence.native_lines]
    )
    unique = Counter(item for item in candidates if len(item) >= 8)
    anchors = [
        item
        for item in candidates
        if len(item) >= 8 and unique[item] == 1 and parsed.count(item) == 1
    ]
    positions = [parsed.index(anchor) for anchor in anchors]
    return len(positions) >= minimum_anchors and any(
        right < left for left, right in zip(positions, positions[1:], strict=False)
    )


class PageEvidenceService:
    def __init__(self, settings: object | None = None) -> None:
        self.sparse_characters = int(setting(settings, "quality_sparse_native_characters", 40))
        self.sparse_image_coverage = float(
            setting(settings, "quality_sparse_max_image_coverage", 0.05)
        )
        self.sparse_ink_ratio = float(setting(settings, "quality_sparse_max_ink_ratio", 0.015))
        self.native_image_coverage = float(
            setting(settings, "quality_native_max_image_coverage", 0.20)
        )
        self.scanned_image_coverage = float(
            setting(settings, "quality_scanned_min_image_coverage", 0.80)
        )
        self.scanned_ink_ratio = float(setting(settings, "quality_scanned_min_ink_ratio", 0.03))

    @staticmethod
    def _block_text(block: dict[str, Any]) -> tuple[str, float, bool, dict[str, str]]:
        lines: list[str] = []
        sizes: list[float] = []
        bold = False
        mappings: dict[str, str] = {}
        for line in block.get("lines", []) if isinstance(block.get("lines"), list) else []:
            if not isinstance(line, dict):
                continue
            parts: list[str] = []
            for span in line.get("spans", []) if isinstance(line.get("spans"), list) else []:
                if not isinstance(span, dict):
                    continue
                text = str(span.get("text", ""))
                font = str(span.get("font", ""))
                parts.append(text)
                try:
                    sizes.append(float(span.get("size", 0.0)))
                except (TypeError, ValueError):
                    pass
                bold = bold or "bold" in font.casefold() or "黑体" in font
                if "wingdings 2" in font.casefold() and "\uf052" in text:
                    mappings["\uf052"] = "☑"
            if parts:
                lines.append("".join(parts).strip())
        return "\n".join(item for item in lines if item), max(sizes, default=0.0), bold, mappings

    @staticmethod
    def _render_metrics(
        page: Any,
    ) -> tuple[
        float,
        int,
        int,
        float,
        int | None,
        list[tuple[float, float, float, float]],
    ]:
        import cv2
        import numpy as np
        import pymupdf
        from PIL import Image

        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(0.6, 0.6), colorspace=pymupdf.csGRAY, alpha=False
        )
        image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("L")
        array = np.asarray(image)
        ink_ratio = float(np.count_nonzero(array < 245) / array.size)
        binary = cv2.threshold(array, 210, 255, cv2.THRESH_BINARY_INV)[1]
        height, width = binary.shape
        horizontal = cv2.morphologyEx(
            binary,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, width // 18), 1)),
        )
        vertical = cv2.morphologyEx(
            binary,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, height // 18))),
        )

        def count_lines(mask: Any, *, horizontal_axis: bool) -> int:
            count = 0
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                _x, _y, item_width, item_height = cv2.boundingRect(contour)
                if (
                    horizontal_axis
                    and item_width >= width * 0.15
                    and item_height <= max(8, height * 0.03)
                ):
                    count += 1
                elif (
                    not horizontal_axis
                    and item_height >= height * 0.15
                    and item_width <= max(8, width * 0.03)
                ):
                    count += 1
            return count

        horizontal_count = count_lines(horizontal, horizontal_axis=True)
        vertical_count = count_lines(vertical, horizontal_axis=False)
        combined = cv2.bitwise_or(horizontal, vertical)
        points = cv2.findNonZero(combined)
        grid_width = 0
        grid_height = 0
        if points is None:
            grid_area = 0.0
        else:
            _x, _y, grid_width, grid_height = cv2.boundingRect(points)
            grid_area = float((grid_width * grid_height) / (width * height))
        regions: list[tuple[float, float, float, float]] = []
        contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, region_width, region_height = cv2.boundingRect(contour)
            area_ratio = region_width * region_height / (width * height)
            if area_ratio < 0.02 or region_width < width * 0.15 or region_height < height * 0.12:
                continue
            regions.append(
                (
                    round(x / width, 6),
                    round(y / height, 6),
                    round((x + region_width) / width, 6),
                    round((y + region_height) / height, 6),
                )
            )
        regions.sort(key=lambda bbox: (bbox[1], bbox[0]))
        detected_rotation: int | None = None
        if (
            vertical_count >= 3
            and vertical_count > horizontal_count * 1.35
            and grid_height > grid_width * 1.10
        ):
            detected_rotation = 90
        elif width > height * 1.10:
            # PDF page rotation has already been applied by the renderer. A
            # genuinely landscape canvas with a wide grid is upright even
            # when a many-column table contains more vertical than horizontal
            # rules.
            detected_rotation = 0
        elif horizontal_count >= 3 and horizontal_count > vertical_count * 1.35:
            detected_rotation = 0
        return (
            ink_ratio,
            horizontal_count,
            vertical_count,
            grid_area,
            detected_rotation,
            regions,
        )

    def inspect(self, source: StoredSource, page_numbers: set[int]) -> dict[int, PageEvidence]:
        if source.mime_type != "application/pdf":
            return {}
        try:
            import pymupdf
        except ImportError:
            return {}
        evidence_by_page: dict[int, PageEvidence] = {}
        try:
            with pymupdf.open(source.path) as document:
                nearby = {
                    candidate
                    for page_number in page_numbers
                    for candidate in (page_number - 1, page_number, page_number + 1)
                    if 1 <= candidate <= document.page_count
                }
                margin_signatures_by_page: dict[int, set[str]] = {}
                raw_blocks: dict[int, list[dict[str, Any]]] = {}
                for page_number in nearby:
                    page = document.load_page(page_number - 1)
                    blocks = [
                        item
                        for item in page.get_text("dict", sort=True).get("blocks", [])
                        if item.get("type") == 0
                    ]
                    raw_blocks[page_number] = blocks
                    for block in blocks:
                        text, _size, _bold, _mappings = self._block_text(block)
                        raw_bbox = block.get("bbox", (0, 0, 0, 0))
                        bbox = tuple(float(value) for value in raw_bbox[:4])
                        if text and (
                            bbox[1] <= page.rect.height * 0.05 or bbox[3] >= page.rect.height * 0.95
                        ):
                            margin_signatures_by_page.setdefault(page_number, set()).add(
                                compact_text(text)
                            )
                for page_number in page_numbers:
                    if not 1 <= page_number <= document.page_count:
                        continue
                    page = document.load_page(page_number - 1)
                    native_text = str(page.get_text("text", sort=True))
                    native_characters = sum(not char.isspace() for char in native_text)
                    page_area = max(1.0, float(page.rect.width * page.rect.height))
                    image_area = sum(
                        max(0.0, float(info["bbox"][2] - info["bbox"][0]))
                        * max(0.0, float(info["bbox"][3] - info["bbox"][1]))
                        for info in page.get_image_info()
                    )
                    image_coverage = min(1.0, image_area / page_area)
                    measure_visual = (
                        native_characters < self.sparse_characters
                        or image_coverage >= self.native_image_coverage
                    )
                    ink_ratio: float | None = None
                    horizontal_lines = vertical_lines = 0
                    grid_area = 0.0
                    grid_regions: list[tuple[float, float, float, float]] = []
                    detected_rotation: int | None = (
                        int(page.rotation) if page.rotation in {90, 180, 270} else None
                    )
                    if measure_visual:
                        (
                            ink_ratio,
                            horizontal_lines,
                            vertical_lines,
                            grid_area,
                            inferred_rotation,
                            grid_regions,
                        ) = self._render_metrics(page)
                        if inferred_rotation is not None:
                            detected_rotation = inferred_rotation

                    if (
                        native_characters < self.sparse_characters
                        and image_coverage < self.sparse_image_coverage
                        and ink_ratio is not None
                        and ink_ratio < self.sparse_ink_ratio
                    ):
                        source_kind = PageSourceKind.SPARSE
                    elif (
                        native_characters >= self.sparse_characters
                        and image_coverage < self.native_image_coverage
                    ):
                        source_kind = PageSourceKind.NATIVE
                    elif native_characters < self.sparse_characters and (
                        image_coverage >= self.scanned_image_coverage
                        or (ink_ratio is not None and ink_ratio >= self.scanned_ink_ratio)
                    ):
                        source_kind = PageSourceKind.SCANNED
                    else:
                        source_kind = PageSourceKind.MIXED

                    body_blocks: list[NativeBlock] = []
                    glyph_mappings: dict[str, str] = {}
                    for block in raw_blocks.get(page_number, []):
                        text, size, bold, mappings = self._block_text(block)
                        if not text:
                            continue
                        raw_bbox = block.get("bbox", (0, 0, 0, 0))
                        bbox_values = tuple(float(value) for value in raw_bbox[:4])
                        if len(bbox_values) != 4:
                            continue
                        bbox = (bbox_values[0], bbox_values[1], bbox_values[2], bbox_values[3])
                        is_margin = (
                            bbox[1] <= page.rect.height * 0.05 or bbox[3] >= page.rect.height * 0.95
                        )
                        signature = compact_text(text)
                        repeated_on_adjacent_page = any(
                            signature in margin_signatures_by_page.get(adjacent, set())
                            for adjacent in (page_number - 1, page_number + 1)
                        )
                        if is_margin and repeated_on_adjacent_page:
                            continue
                        glyph_mappings.update(mappings)
                        body_blocks.append(
                            NativeBlock(text=text, bbox=bbox, max_font_size=size, bold=bold)
                        )
                    body_text = "\n\n".join(block.text for block in body_blocks)
                    lines = [
                        line.strip()
                        for block in body_blocks
                        for line in block.text.splitlines()
                        if line.strip()
                    ]
                    evidence_by_page[page_number] = PageEvidence(
                        page_number=page_number,
                        source_kind=source_kind,
                        native_text=native_text,
                        native_body_text=body_text,
                        native_lines=lines,
                        native_blocks=body_blocks,
                        native_text_characters=native_characters,
                        image_coverage_ratio=round(image_coverage, 6),
                        visual_ink_ratio=round(ink_ratio, 6) if ink_ratio is not None else None,
                        detected_rotation_degrees=detected_rotation,
                        glyph_mappings=glyph_mappings,
                        horizontal_grid_lines=horizontal_lines,
                        vertical_grid_lines=vertical_lines,
                        grid_area_ratio=round(grid_area, 6),
                        page_width=float(page.rect.width),
                        page_height=float(page.rect.height),
                        grid_regions=grid_regions,
                    )
        except (ImportError, OSError, RuntimeError, ValueError):
            return {}
        return evidence_by_page


def safe_cjk_compatibility_map(content: str, native_text: str) -> dict[str, str]:
    mappings: dict[str, str] = {}
    compact_content = compact_text(content)
    compact_native = compact_text(native_text)
    matcher = SequenceMatcher(None, compact_content, compact_native, autojunk=False)
    for operation, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if operation != "replace" or left_end - left_start != 1 or right_end - right_start != 1:
            continue
        character = compact_content[left_start:left_end]
        target = compact_native[right_start:right_end]
        normalized = unicodedata.normalize("NFKC", character)
        if (
            len(normalized) == 1
            and normalized != character
            and normalized == target
            and "CJK" in unicodedata.name(normalized, "")
        ):
            mappings[character] = normalized
    return mappings
