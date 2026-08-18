"""Materialize content evidence from the proven page-oriented parser pipeline."""

from __future__ import annotations

import hashlib
import re
import statistics
from pathlib import Path

from app.models.document_ir import (
    ContentEvidenceIR,
    ContentLinksIR,
    ContentQualitySummary,
    ContentRenderings,
    ContentSourceIR,
    ContentUnitIR,
    ElementEvidence,
    ElementQuality,
    EvidenceSource,
    EvidenceSourceKind,
    IRWarning,
    LogicalTableIR,
    NormalizedBBox,
    ParseRuntimeIR,
    RegionIR,
    RegionType,
    SourceKind,
    TableCellIR,
    TableIR,
    TextBlockIR,
    UnitDiagnostics,
    UnitType,
)
from app.models.parse_result import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    QualityVerdict,
)
from app.models.source import StoredSource
from app.services.evidence_service import PageEvidence, compact_text
from app.services.table_service import TableCellFragment, TableFragment
from app.utils.settings import setting

_MARKDOWN_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_MARKER_LINE = re.compile(r"^\s*<!--.*-->\s*$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _bbox(values: tuple[float, float, float, float] | None) -> NormalizedBBox | None:
    if values is None:
        return None
    left, top, right, bottom = values
    try:
        return NormalizedBBox(left=left, top=top, right=right, bottom=bottom)
    except ValueError:
        return None


def _native_bbox(
    values: tuple[float, float, float, float], evidence: PageEvidence
) -> NormalizedBBox | None:
    if evidence.page_width <= 0 or evidence.page_height <= 0:
        return None
    left, top, right, bottom = values
    return _bbox(
        (
            max(0.0, min(1.0, left / evidence.page_width)),
            max(0.0, min(1.0, top / evidence.page_height)),
            max(0.0, min(1.0, right / evidence.page_width)),
            max(0.0, min(1.0, bottom / evidence.page_height)),
        )
    )


def _source_kind(value: str) -> EvidenceSourceKind:
    normalized = value.casefold()
    if "qwen" in normalized or "vlm" in normalized:
        return EvidenceSourceKind.QWEN
    if "glm" in normalized or "sdk" in normalized:
        return EvidenceSourceKind.GLM
    if "native" in normalized or "pymupdf" in normalized:
        return EvidenceSourceKind.NATIVE
    return EvidenceSourceKind.DOCLING


def _cell_evidence(
    *,
    text: str,
    fragment: TableFragment,
    row: int,
    column: int,
) -> tuple[ElementQuality, ElementEvidence]:
    base_source = _source_kind(fragment.source_kind)
    values: dict[str, str | None] = {}
    if row < len(fragment.cell_evidence) and column < len(fragment.cell_evidence[row]):
        values = fragment.cell_evidence[row][column]
    sources = {
        _source_kind(name)
        for name, value in values.items()
        if name != "final" and value is not None
    }
    sources.add(base_source)
    selected = [
        source
        for name, value in values.items()
        if name != "final"
        and value is not None
        and compact_text(value) == compact_text(text)
        and (source := _source_kind(name))
    ]
    selected_source = selected[0] if selected else base_source
    supporting = sorted(set(selected), key=lambda item: item.value)
    quality = ElementQuality.CONFIRMED if len(supporting) >= 2 else ElementQuality.SELECTED
    if not fragment.valid:
        quality = ElementQuality.CONFLICTED
    return quality, ElementEvidence(
        selected_source=selected_source,
        supporting_sources=supporting,
        sources=[
            EvidenceSource(source=source) for source in sorted(sources, key=lambda x: x.value)
        ],
        reason_codes=list(fragment.invalid_reasons),
    )


def _fragment_cells(fragment: TableFragment) -> list[TableCellIR]:
    explicit = {(cell.row, cell.column): cell for cell in fragment.cells}
    occupied: set[tuple[int, int]] = set()
    output: list[TableCellIR] = []
    for row in range(fragment.num_rows):
        values = fragment.rows[row] if row < len(fragment.rows) else []
        for column in range(fragment.num_cols):
            if (row, column) in occupied:
                continue
            source: TableCellFragment | None = explicit.get((row, column))
            row_span = source.row_span if source else 1
            column_span = source.column_span if source else 1
            for occupied_row in range(row, min(fragment.num_rows, row + row_span)):
                for occupied_column in range(column, min(fragment.num_cols, column + column_span)):
                    occupied.add((occupied_row, occupied_column))
            text = source.text if source else (values[column] if column < len(values) else "")
            quality, evidence = _cell_evidence(text=text, fragment=fragment, row=row, column=column)
            if not text and not fragment.valid:
                quality = ElementQuality.MISSING
            output.append(
                TableCellIR(
                    cell_id=f"{fragment.fragment_id}-r{row}-c{column}",
                    row=row,
                    column=column,
                    row_span=row_span,
                    column_span=column_span,
                    bbox=_bbox(source.normalized_bbox if source else None),
                    text=text,
                    is_column_header=(source.is_column_header if source else row == 0),
                    is_row_header=source.is_row_header if source else False,
                    quality=quality,
                    evidence=evidence,
                )
            )
    return output


def _plain_non_table_text(markdown: str) -> str:
    return "\n".join(
        line
        for line in markdown.splitlines()
        if not _MARKDOWN_TABLE_LINE.match(line) and not _MARKER_LINE.match(line)
    ).strip()


def _coverage(inner: NormalizedBBox | None, outer: NormalizedBBox | None) -> float:
    if inner is None or outer is None:
        return 0.0
    width = max(0.0, min(inner.right, outer.right) - max(inner.left, outer.left))
    height = max(0.0, min(inner.bottom, outer.bottom) - max(inner.top, outer.top))
    area = max(0.0, inner.right - inner.left) * max(0.0, inner.bottom - inner.top)
    return width * height / area if area > 0 else 0.0


def _region_order(region: RegionIR) -> tuple[float, float, int]:
    if region.bbox is not None:
        return region.bbox.top, region.bbox.left, region.reading_order
    return 2.0, 2.0, region.reading_order


def _warning_count(details: dict[str, object] | None) -> int | None:
    value = details.get("count") if details else None
    return value if isinstance(value, int) and value >= 0 else None


class DocumentIRService:
    def __init__(self, settings: object | None = None) -> None:
        self.settings = settings

    def build(
        self,
        result: DocumentParseResult,
        source: StoredSource,
        evidence_by_page: dict[int, PageEvidence],
    ) -> ContentEvidenceIR:
        fragments = [
            fragment for fragment in result._table_fragments if isinstance(fragment, TableFragment)
        ]
        tables = [self._table(fragment) for fragment in fragments]
        tables_by_page: dict[int, list[TableIR]] = {}
        for table in tables:
            tables_by_page.setdefault(table.source_units[0], []).append(table)
        pictures_by_page: dict[int, list[object]] = {}
        for picture in result._picture_candidates:
            page_number = getattr(picture, "page_number", None)
            if isinstance(page_number, int):
                pictures_by_page.setdefault(page_number, []).append(picture)

        units = [
            self._page(
                page,
                evidence_by_page.get(page.page_number),
                tables_by_page,
                result._visual_page_irs.get(page.page_number),
                pictures_by_page.get(page.page_number, []),
            )
            for page in sorted(result.pages, key=lambda item: item.page_number)
        ]
        if source.mime_type.startswith("image/") and len(units) == 1:
            units[0].unit_type = UnitType.IMAGE
        logical_groups: dict[str, list[TableIR]] = {}
        for table in tables:
            if table.logical_table_id:
                logical_groups.setdefault(table.logical_table_id, []).append(table)
        logical_tables = [
            LogicalTableIR(
                logical_table_id=logical_id,
                fragment_table_ids=[table.table_id for table in group],
                source_units=sorted({unit for table in group for unit in table.source_units}),
            )
            for logical_id, group in sorted(logical_groups.items())
        ]

        summary = result.quality_summary
        qwen_calls = summary.qwen_calls if summary is not None else 0
        return ContentEvidenceIR(
            status="partial" if result.route_summary.failed_pages else "completed",
            source=ContentSourceIR(
                content_id=result.document_id,
                source_sha256=_sha256(source.path),
                filename=result.filename,
                mime_type=result.mime_type,
                kind=SourceKind.PDF if source.mime_type == "application/pdf" else SourceKind.IMAGE,
                size_bytes=source.size_bytes,
                unit_count=result.page_count,
                page_count=(
                    result.page_count
                    if source.mime_type in {"application/pdf", "image/tiff"}
                    else None
                ),
            ),
            units=units,
            tables=tables,
            logical_tables=logical_tables,
            renderings=ContentRenderings(
                markdown=result.markdown,
                plain_text=result.plain_text,
            ),
            diagnostics=ContentQualitySummary(
                trusted_units=summary.trusted_pages if summary else 0,
                degraded_units=summary.degraded_pages if summary else 0,
                untrusted_units=summary.untrusted_pages if summary else 0,
                repaired_units=summary.repaired_pages if summary else 0,
                visual_units=summary.visual_pages if summary else 0,
                unresolved_visual_conflicts=(summary.unresolved_visual_conflicts if summary else 0),
            ),
            warnings=[
                IRWarning(
                    code=warning.code,
                    severity=warning.severity.value,
                    unit_index=warning.page_number,
                    count=_warning_count(warning.details),
                )
                for warning in result.warnings
            ],
            runtime=ParseRuntimeIR(
                profile=result.pipeline.profile,  # type: ignore[arg-type]
                primary_backend=result.pipeline.primary,
                ocr_backend=result.pipeline.ocr,
                visual_backend=result.pipeline.vlm,
                parser_version=str(setting(self.settings, "version", "0.3.0")),
                input_bytes=result.usage.input_bytes,
                duration_ms=result.usage.duration_ms,
                qwen_calls=qwen_calls,
            ),
            links=ContentLinksIR(
                job=f"/v1/content/jobs/{result.document_id}",
                events=f"/v1/content/jobs/{result.document_id}/events",
                result=f"/v1/content/jobs/{result.document_id}/result",
                assets=f"/v1/content/jobs/{result.document_id}/assets",
                bundle=f"/v1/content/jobs/{result.document_id}/bundle",
            ),
            created_at=result.created_at,
        )

    @staticmethod
    def _table(fragment: TableFragment) -> TableIR:
        quality = ElementQuality.COMPLETE if fragment.valid else ElementQuality.CONFLICTED
        return TableIR(
            table_id=fragment.fragment_id,
            unit_id=f"p{fragment.page_number}",
            region_id=f"{fragment.fragment_id}-region",
            source_units=[fragment.page_number],
            bbox=_bbox(fragment.normalized_bbox),
            caption=fragment.caption,
            unit_text=fragment.unit_text,
            row_count=fragment.num_rows,
            column_count=fragment.num_cols,
            header_rows=[0] if fragment.has_column_header and fragment.num_rows else [],
            cells=_fragment_cells(fragment),
            logical_table_id=fragment.logical_table_id,
            quality=quality,
            reason_codes=list(fragment.invalid_reasons),
        )

    @staticmethod
    def _page(
        page: PageParseResult,
        evidence: PageEvidence | None,
        tables_by_page: dict[int, list[TableIR]],
        visual_page_ir: object | None,
        picture_candidates: list[object],
    ) -> ContentUnitIR:
        page_number = int(page.page_number)
        page_id = f"p{page_number}"
        page_tables = tables_by_page.get(page_number, [])
        blocks: list[TextBlockIR] = []
        regions: list[RegionIR] = []
        if evidence and evidence.native_blocks:
            ordered = evidence._blocks_in_reading_order()
            sizes = [block.max_font_size for block in ordered if block.max_font_size > 0]
            median = statistics.median(sizes) if sizes else 0.0
            for index, native_block in enumerate(ordered, 1):
                block_id = f"{page_id}-b{index}"
                region_id = f"{page_id}-r{index}"
                block_type = (
                    RegionType.HEADING
                    if median > 0
                    and (native_block.bold or native_block.max_font_size >= median * 1.28)
                    else RegionType.TEXT
                )
                bbox = _native_bbox(native_block.bbox, evidence)
                if any(_coverage(bbox, table.bbox) >= 0.5 for table in page_tables):
                    continue
                item = TextBlockIR(
                    block_id=block_id,
                    unit_id=page_id,
                    region_id=region_id,
                    block_type=block_type,
                    bbox=bbox,
                    reading_order=index,
                    text=native_block.text,
                    quality=ElementQuality.COMPLETE,
                    evidence=ElementEvidence(
                        selected_source=EvidenceSourceKind.NATIVE,
                        supporting_sources=[EvidenceSourceKind.NATIVE],
                        sources=[EvidenceSource(source=EvidenceSourceKind.NATIVE)],
                    ),
                )
                blocks.append(item)
                regions.append(
                    RegionIR(
                        region_id=region_id,
                        unit_id=page_id,
                        region_type=block_type,
                        bbox=bbox,
                        reading_order=index,
                        block_ids=[block_id],
                    )
                )
        else:
            text = _plain_non_table_text(str(getattr(page, "content", "") or ""))
            if text:
                block_id = f"{page_id}-b1"
                region_id = f"{page_id}-r1"
                source = _source_kind(str(getattr(page, "backend", "") or "docling"))
                blocks.append(
                    TextBlockIR(
                        block_id=block_id,
                        unit_id=page_id,
                        region_id=region_id,
                        reading_order=1,
                        text=text,
                        quality=ElementQuality.SELECTED,
                        evidence=ElementEvidence(
                            selected_source=source,
                            supporting_sources=[source],
                            sources=[EvidenceSource(source=source)],
                        ),
                    )
                )
                regions.append(
                    RegionIR(
                        region_id=region_id,
                        unit_id=page_id,
                        region_type=RegionType.TEXT,
                        reading_order=1,
                        block_ids=[block_id],
                        quality=ElementQuality.SELECTED,
                    )
                )
        for offset, table in enumerate(page_tables, len(regions) + 1):
            regions.append(
                RegionIR(
                    region_id=table.region_id,
                    unit_id=page_id,
                    region_type=RegionType.TABLE,
                    bbox=table.bbox,
                    reading_order=offset,
                    table_ids=[table.table_id],
                    quality=table.quality,
                )
            )

        signature = getattr(visual_page_ir, "signature", None)
        visual_only_names = getattr(signature, "visual_only_names", []) if signature else []
        if signature is not None and bool(getattr(signature, "has_seal", False)):
            regions.append(
                RegionIR(
                    region_id=f"{page_id}-seal",
                    unit_id=page_id,
                    region_type=RegionType.SEAL,
                    reading_order=len(regions) + 1,
                    quality=ElementQuality.SELECTED,
                )
            )
        handwriting_region_id: str | None = None
        if signature is not None and bool(getattr(signature, "has_handwriting", False)):
            handwriting_region_id = f"{page_id}-handwriting"
            handwriting_blocks: list[str] = []
            for name in visual_only_names:
                text = str(name or "").strip()
                if not text:
                    continue
                block_id = f"{handwriting_region_id}-b{len(handwriting_blocks) + 1}"
                handwriting_blocks.append(block_id)
                blocks.append(
                    TextBlockIR(
                        block_id=block_id,
                        unit_id=page_id,
                        region_id=handwriting_region_id,
                        block_type=RegionType.HANDWRITING,
                        reading_order=len(regions) + 1,
                        text=text,
                        quality=ElementQuality.SELECTED,
                        evidence=ElementEvidence(
                            selected_source=EvidenceSourceKind.QWEN,
                            supporting_sources=[EvidenceSourceKind.QWEN],
                            sources=[EvidenceSource(source=EvidenceSourceKind.QWEN)],
                            reason_codes=["visual_only_handwriting"],
                        ),
                    )
                )
            regions.append(
                RegionIR(
                    region_id=handwriting_region_id,
                    unit_id=page_id,
                    region_type=RegionType.HANDWRITING,
                    reading_order=len(regions) + 1,
                    block_ids=handwriting_blocks,
                    quality=ElementQuality.SELECTED,
                )
            )
        elif picture_candidates:
            for index, picture in enumerate(picture_candidates, 1):
                regions.append(
                    RegionIR(
                        region_id=f"{page_id}-image-{index}",
                        unit_id=page_id,
                        region_type=RegionType.IMAGE,
                        bbox=_bbox(getattr(picture, "normalized_bbox", None)),
                        reading_order=len(regions) + 1,
                        quality=ElementQuality.COMPLETE,
                    )
                )
        elif "<!-- image -->" in str(getattr(page, "content", "") or ""):
            regions.append(
                RegionIR(
                    region_id=f"{page_id}-image",
                    unit_id=page_id,
                    region_type=RegionType.IMAGE,
                    reading_order=len(regions) + 1,
                    quality=ElementQuality.SELECTED,
                )
            )

        regions.sort(key=_region_order)
        order_by_region: dict[str, int] = {}
        for reading_order, region in enumerate(regions, 1):
            region.reading_order = reading_order
            order_by_region[region.region_id] = reading_order
        for ir_block in blocks:
            ir_block.reading_order = order_by_region.get(ir_block.region_id, ir_block.reading_order)

        diagnostics = getattr(page, "diagnostics", None)
        visual = diagnostics.visual_fusion if diagnostics else None
        source_kind = diagnostics.source_kind.value if diagnostics else "native"
        verdict = (
            diagnostics.quality_verdict.value
            if diagnostics
            else (
                QualityVerdict.UNTRUSTED.value
                if page.status != PageStatus.COMPLETED
                else QualityVerdict.TRUSTED.value
            )
        )
        return ContentUnitIR(
            unit_id=page_id,
            unit_type=UnitType.PAGE,
            index=page_number,
            width=evidence.page_width if evidence and evidence.page_width > 0 else None,
            height=evidence.page_height if evidence and evidence.page_height > 0 else None,
            rotation_degrees=(diagnostics.detected_rotation_degrees if diagnostics else None),
            status=page.status.value,
            regions=regions,
            blocks=blocks,
            table_ids=[table.table_id for table in page_tables],
            renderings=ContentRenderings(
                markdown=str(getattr(page, "content", "") or ""),
                plain_text=str(getattr(page, "plain_text", "") or ""),
            ),
            diagnostics=UnitDiagnostics(
                source_kind=source_kind,  # type: ignore[arg-type]
                quality_verdict=verdict,  # type: ignore[arg-type]
                selected_strategy=(
                    diagnostics.selected_strategy.value if diagnostics else "docling"
                ),
                native_text_characters=(
                    diagnostics.native_text_characters if diagnostics else None
                ),
                visual_ink_ratio=diagnostics.visual_ink_ratio if diagnostics else None,
                image_coverage_ratio=(diagnostics.image_coverage_ratio if diagnostics else None),
                detected_rotation_degrees=(
                    diagnostics.detected_rotation_degrees if diagnostics else None
                ),
                warning_codes=[warning.code for warning in getattr(page, "warnings", [])],
                qwen_calls=visual.qwen_calls if visual else None,
                qwen_duration_ms=visual.qwen_duration_ms if visual else None,
                unresolved_conflicts=visual.unresolved_conflicts if visual else None,
                truncated_calls=visual.truncated_calls if visual else None,
            ),
            duration_ms=int(getattr(page, "duration_ms", 0) or 0),
        )
