"""Image, Office-media, and standalone-video result processing."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import posixpath
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.etree import ElementTree

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.models.content_result import (
    AssetKind,
    AssetLocation,
    AssetRole,
    AssetStatus,
    ContentAsset,
    ContentLinks,
    ContentParseResult,
    ContentQualitySummary,
    ContentRegion,
    ContentRenderings,
    ContentSource,
    ContentUnit,
    ElementProvenance,
    ElementQuality,
    ParseRuntime,
    ParseWarning,
    ProvenanceSource,
    ProvenanceSourceKind,
    RegionType,
    SourceKind,
    TextBlock,
    UnitDiagnostics,
    UnitType,
    VideoAnalysis,
    VideoKeyframe,
    VideoScene,
    VisualAnalysis,
    VisualClassification,
)
from app.models.parse_options import ContentParseOptions
from app.models.parse_result import (
    DocumentParseResult,
    PageDiagnostics,
    PageParseResult,
    PageSourceKind,
    ParsePipeline,
    ParseUsage,
    QualitySummary,
    RouteSummary,
)
from app.models.source import StoredSource
from app.parsers.base import (
    DocumentParser,
    ParserCancelledError,
    ParserError,
    ParserUnavailableError,
    ProgressCallback,
)
from app.parsers.ollama_vlm import OllamaVisualAdapter
from app.security.file_validation import DOCX_MIME, PPTX_MIME, detect_mime_type
from app.services.evidence_service import PageEvidence
from app.services.storage_service import StorageService
from app.utils.settings import setting

logger = logging.getLogger(__name__)

_RELATIONSHIP_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SCENE_TIME_RE = re.compile(r"pts_time:(?P<time>\d+(?:\.\d+)?)")
_SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=(?P<score>\d+(?:\.\d+)?)")


class _VisualResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: VisualClassification
    summary: str = Field(min_length=1)
    detailed_description: str = ""
    visible_text: str = ""
    scene: str | None = None
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    language: Literal["zh-CN", "en", "auto"] | None = None
    uncertainties: list[str] = Field(default_factory=list)


class _VideoSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class EmbeddedImage:
    content: bytes
    filename: str
    mime_type: str
    unit_index: int
    relationship_id: str
    placement_index: int = 1


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    duration_ms: int
    width: int
    height: int
    codec: str | None
    frame_rate: float | None


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_id(digest: str) -> str:
    return f"asset_{digest[:26]}"


def _cancelled(cancel_event: object | None) -> bool:
    return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())


async def _notify(
    callback: ProgressCallback | None,
    current: int,
    total: int,
    message: str,
) -> None:
    if callback is None:
        return
    value = callback(current, total, message)
    if asyncio.iscoroutine(value):
        await value


class MultimodalService:
    """Attach downloadable visual evidence and handle non-page content."""

    def __init__(
        self,
        settings: object | None,
        storage: StorageService,
        vlm: OllamaVisualAdapter,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.vlm = vlm
        self.max_assets = int(setting(settings, "max_assets_per_content", 200))
        self.max_asset_bytes = int(setting(settings, "max_asset_bytes", 50 * 1024 * 1024))
        self.max_extracted_bytes = int(
            setting(settings, "max_extracted_asset_bytes", 400 * 1024 * 1024)
        )
        self.max_asset_image_pixels = int(
            setting(settings, "max_asset_image_pixels", 100_000_000)
        )
        self.media_vlm_max_pixels = int(
            setting(settings, "media_vlm_max_pixels", 4_500_000)
        )
        self.max_keyframes = int(setting(settings, "video_max_keyframes", 24))
        self.min_spacing = float(setting(settings, "video_min_frame_spacing_seconds", 2.0))
        self.scene_threshold = float(setting(settings, "video_scene_threshold", 0.30))
        self.ffmpeg_timeout = float(setting(settings, "ffmpeg_timeout_seconds", 300.0))
        self.ffmpeg_threads = int(setting(settings, "ffmpeg_threads", 2))
        self.ffmpeg_max_alloc_bytes = int(
            setting(settings, "ffmpeg_max_alloc_bytes", 512 * 1024 * 1024)
        )
        self._ffmpeg_semaphore = asyncio.Semaphore(
            int(setting(settings, "ffmpeg_max_concurrency", 1))
        )

    def _links(self, content_id: str) -> ContentLinks:
        base = f"/v1/content/jobs/{content_id}"
        return ContentLinks(
            job=base,
            events=f"{base}/events",
            result=f"{base}/result",
            assets=f"{base}/assets",
            bundle=f"{base}/bundle",
        )

    @staticmethod
    def classify_image(evidence: PageEvidence | None, visible_text: str) -> VisualClassification:
        """Classify using observed text, ink, and image-coverage signals."""

        characters = len("".join(visible_text.split()))
        if evidence is None:
            # Raster inputs do not have PyMuPDF page metrics. Their completed
            # Docling/OCR text is still a measured routing signal.
            if characters >= 80:
                return VisualClassification.DOCUMENT
            if characters >= 15:
                return VisualClassification.MIXED
            if characters == 0:
                return VisualClassification.VISUAL
            return VisualClassification.UNKNOWN
        image_coverage = evidence.image_coverage_ratio or 0.0
        ink_ratio = evidence.visual_ink_ratio or 0.0
        if characters >= 80 or (characters >= 40 and ink_ratio >= 0.02 and image_coverage >= 0.55):
            return VisualClassification.DOCUMENT
        if characters >= 15:
            return VisualClassification.MIXED
        if characters < 15 and (image_coverage >= 0.4 or ink_ratio < 0.08):
            return VisualClassification.VISUAL
        return VisualClassification.UNKNOWN

    @staticmethod
    def _language_instruction(language: Literal["zh-CN", "en", "auto"]) -> str:
        return {
            "zh-CN": (
                "Write every natural-language output field in Simplified Chinese and set "
                "the language field to zh-CN. Do not answer in English."
            ),
            "en": (
                "Write every natural-language output field in English and set the "
                "language field to en."
            ),
            "auto": "Use the main language visible in the image or sampled frames.",
        }[language]

    @staticmethod
    def _measured_document_analysis(
        visible_text: str,
        *,
        language: Literal["zh-CN", "en", "auto"],
    ) -> VisualAnalysis:
        english = language == "en"
        return VisualAnalysis(
            classification=VisualClassification.DOCUMENT,
            summary=(
                "This image primarily contains parseable document content."
                if english
                else "该图片主要包含可解析的文档内容。"
            ),
            detailed_description=(
                "Its content was extracted through the document layout and text path."
                if english
                else "内容已通过文档布局与文字解析路径提取。"
            ),
            visible_text=visible_text,
            language=language,
            model="measured-layout-ocr-router",
        )

    async def _analyze_embedded_image(
        self,
        content: bytes,
        asset: ContentAsset,
        options: ContentParseOptions,
        *,
        content_id: str,
        image_parser: DocumentParser | None,
        cancel_event: object | None,
    ) -> VisualAnalysis:
        """Route a child image without invoking recursive media discovery."""

        visible_text = ""
        classification: VisualClassification | None = None
        if image_parser is not None:
            asset_path = self.storage.asset_path(content_id, asset.asset_id)
            if asset_path is None:
                raise ParserError("persisted embedded image asset is unavailable")
            child_source = StoredSource(
                path=asset_path,
                filename=asset.filename,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
                page_count=1,
            )
            child_options = options.model_copy(
                update={"unit_range": "1", "include_renderings": True}
            )
            try:
                child = await image_parser.parse(
                    child_source,
                    child_options,
                    document_id=f"{content_id}_{asset.asset_id}",
                    progress_callback=None,
                    cancel_event=cancel_event,
                )
                visible_text = child.plain_text
                classification = self.classify_image(None, visible_text)
            except (ParserCancelledError, asyncio.CancelledError):
                raise
            except ParserError as exc:
                logger.info(
                    "embedded document-image routing fell back to VLM",
                    extra={"error_code": type(exc).__name__},
                )

        if classification == VisualClassification.DOCUMENT:
            return self._measured_document_analysis(
                visible_text,
                language=options.description_language,
            )

        analysis = await self._describe(
            content,
            language=options.description_language,
            classification=classification,
        )
        if visible_text and len(visible_text.strip()) > len(analysis.visible_text.strip()):
            analysis.visible_text = visible_text
        return analysis

    async def _describe(
        self,
        image: bytes,
        *,
        language: Literal["zh-CN", "en", "auto"],
        classification: VisualClassification | None = None,
    ) -> VisualAnalysis:
        if not self.vlm.enabled:
            raise ParserUnavailableError("VLM is required for visual media analysis")
        language_instruction = self._language_instruction(language)
        prompt = (
            "Analyze only what is visibly present in this image. "
            "Classify it as document, visual, mixed, or unknown; provide a concise summary, "
            "a detailed description, visible text, scene, objects, actions, and explicit "
            "uncertainties. Report the description language as zh-CN or en. "
            f"Do not infer hidden facts. {language_instruction}"
        )
        normalized = await asyncio.to_thread(self._normalized_png, image)
        completion = await self.vlm.complete_structured(
            [normalized],
            prompt,
            _VisualResponse,
            max_tokens=int(setting(self.settings, "media_vlm_max_tokens", 4096)),
            reasoning_effort=str(
                setting(self.settings, "media_vlm_reasoning_effort", "none")
            ),
        )
        response = completion.value
        return VisualAnalysis(
            classification=classification or response.classification,
            summary=response.summary,
            detailed_description=response.detailed_description,
            visible_text=response.visible_text,
            scene=response.scene,
            objects=response.objects,
            actions=response.actions,
            language=response.language or language,
            model=self.vlm.model,
            uncertainties=response.uncertainties,
        )

    def _normalized_png(self, content: bytes) -> bytes:
        """Return a bounded RGB PNG accepted consistently by VLM backends and browsers."""

        try:
            with Image.open(io.BytesIO(content)) as source:
                source.seek(0)
                width, height = source.size
                if width * height > self.max_asset_image_pixels:
                    raise ParserError("image dimensions exceed the asset pixel safety limit")
                image = source.convert("RGB")
                if width * height > self.media_vlm_max_pixels:
                    scale = math.sqrt(self.media_vlm_max_pixels / (width * height))
                    image.thumbnail(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue()
        except ParserError:
            raise
        except (OSError, ValueError) as exc:
            raise ParserError("image cannot be normalized for visual analysis") from exc

    async def _persist_image_asset(
        self,
        content_id: str,
        content: bytes,
        *,
        filename: str,
        role: AssetRole,
        location: AssetLocation,
        parent_asset_id: str | None = None,
        derived: bool = False,
        derived_category: Literal["keyframes", "previews"] = "keyframes",
    ) -> tuple[ContentAsset, bool]:
        digest = _sha256_bytes(content)
        identifier = _asset_id(digest)
        try:
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                detected = Image.MIME.get(image.format or "")
        except (OSError, ValueError) as exc:
            raise ParserError("embedded image is invalid") from exc
        if width * height > self.max_asset_image_pixels:
            raise ParserError("image dimensions exceed the asset pixel safety limit")
        mime_type = (
            detected or detect_mime_type(content[:64], suffix=Path(filename).suffix) or "image/png"
        )
        existing = self.storage.asset_path(content_id, identifier)
        if existing is None:
            await self.storage.write_asset(
                content_id,
                identifier,
                filename,
                content,
                derived=derived,
                derived_category=derived_category,
            )
        return (
            ContentAsset(
                asset_id=identifier,
                kind=AssetKind.IMAGE,
                role=role,
                mime_type=mime_type,
                sha256=digest,
                size_bytes=len(content),
                filename=filename,
                width=width,
                height=height,
                parent_asset_id=parent_asset_id,
                locations=[location],
                download_url=f"/v1/content/jobs/{content_id}/assets/{identifier}",
            ),
            existing is None,
        )

    async def _preview_asset(
        self,
        content_id: str,
        original: ContentAsset,
        content: bytes,
        location: AssetLocation,
    ) -> ContentAsset | None:
        if original.mime_type in {"image/png", "image/jpeg", "image/webp"}:
            return None
        normalized = await asyncio.to_thread(self._normalized_png, content)
        preview, _ = await self._persist_image_asset(
            content_id,
            normalized,
            filename=f"{Path(original.filename).stem}-preview.png",
            role=AssetRole.PREVIEW,
            location=location,
            parent_asset_id=original.asset_id,
            derived=True,
            derived_category="previews",
        )
        return preview

    @staticmethod
    def _merge_asset(assets: list[ContentAsset], candidate: ContentAsset) -> ContentAsset:
        existing = next((item for item in assets if item.sha256 == candidate.sha256), None)
        if existing is None:
            assets.append(candidate)
            return candidate
        known = {location.model_dump_json() for location in existing.locations}
        existing.locations.extend(
            location for location in candidate.locations if location.model_dump_json() not in known
        )
        return existing

    async def enrich_page_content(
        self,
        parse_result: ContentParseResult,
        result: DocumentParseResult,
        source: StoredSource,
        evidence_by_page: dict[int, PageEvidence],
        options: ContentParseOptions,
        *,
        image_parser: DocumentParser | None = None,
        cancel_event: object | None = None,
    ) -> None:
        """Add a source image or semantic PDF picture crops to the parse result."""

        if source.mime_type.startswith("image/"):
            content = await asyncio.to_thread(source.path.read_bytes)
            asset, _ = await self._persist_image_asset(
                result.document_id,
                content,
                filename=source.filename,
                role=AssetRole.SOURCE,
                location=AssetLocation(unit_id=parse_result.units[0].unit_id, page_number=1),
            )
            for unit in parse_result.units[1:]:
                asset.locations.append(
                    AssetLocation(unit_id=unit.unit_id, page_number=unit.index)
                )
            classification = self.classify_image(evidence_by_page.get(1), result.plain_text)
            if classification == VisualClassification.DOCUMENT:
                asset.visual_analysis = self._measured_document_analysis(
                    result.plain_text,
                    language=options.description_language,
                )
            else:
                asset.visual_analysis = await self._describe(
                    content,
                    language=options.description_language,
                    classification=classification,
                )
            asset = self._merge_asset(parse_result.assets, asset)
            for unit in parse_result.units:
                if asset.asset_id not in unit.asset_ids:
                    unit.asset_ids.append(asset.asset_id)
            analysis = asset.visual_analysis
            if analysis is None:
                raise ParserError("source image analysis produced no result")
            parse_result.visual_analysis = analysis
            preview = await self._preview_asset(
                result.document_id,
                asset,
                content,
                AssetLocation(unit_id=parse_result.units[0].unit_id, page_number=1),
            )
            if preview is not None:
                preview.visual_analysis = analysis
                for unit in parse_result.units[1:]:
                    preview.locations.append(
                        AssetLocation(unit_id=unit.unit_id, page_number=unit.index)
                    )
                preview = self._merge_asset(parse_result.assets, preview)
                for unit in parse_result.units:
                    if preview.asset_id not in unit.asset_ids:
                        unit.asset_ids.append(preview.asset_id)
            description = analysis.detailed_description or analysis.summary
            if classification in {
                VisualClassification.VISUAL,
                VisualClassification.MIXED,
                VisualClassification.UNKNOWN,
            }:
                parse_result.renderings.markdown = (
                    f"{parse_result.renderings.markdown}\n\n## 图片描述\n\n{description}".strip()
                )
                parse_result.renderings.plain_text = (
                    f"{parse_result.renderings.plain_text}\n\n图片描述：{description}".strip()
                )

        if source.mime_type == "application/pdf":
            await self._enrich_pdf_pictures(
                parse_result,
                result,
                source,
                options,
                image_parser=image_parser,
                cancel_event=cancel_event,
            )

    async def _enrich_pdf_pictures(
        self,
        parse_result: ContentParseResult,
        result: DocumentParseResult,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        image_parser: DocumentParser | None,
        cancel_event: object | None,
    ) -> None:
        candidates = list(result._picture_candidates)[: self.max_assets]
        for index, picture in enumerate(candidates, start=1):
            current_asset: ContentAsset | None = None
            page_number = int(getattr(picture, "page_number", 0))
            bbox = getattr(picture, "normalized_bbox", None)
            if page_number < 1 or not isinstance(bbox, tuple):
                continue
            unit = next((item for item in parse_result.units if item.index == page_number), None)
            if unit is None:
                continue
            try:
                page_image = await self.vlm._render(source, page_number, options.profile.value)
                crop = await asyncio.to_thread(self.vlm._crop_normalized_image, page_image, bbox)
                asset, _ = await self._persist_image_asset(
                    result.document_id,
                    crop,
                    filename=f"page-{page_number}-image-{index}.png",
                    role=AssetRole.PAGE_CROP,
                    location=AssetLocation(unit_id=unit.unit_id, page_number=page_number),
                )
                current_asset = self._merge_asset(parse_result.assets, asset)
                if (
                    current_asset.visual_analysis is None
                    and "embedded_image_analysis_failed"
                    not in current_asset.warning_codes
                ):
                    current_asset.visual_analysis = await self._analyze_embedded_image(
                        crop,
                        current_asset,
                        options,
                        content_id=result.document_id,
                        image_parser=image_parser,
                        cancel_event=cancel_event,
                    )
                if current_asset.asset_id not in unit.asset_ids:
                    unit.asset_ids.append(current_asset.asset_id)
            except (OSError, ValueError, ParserError) as exc:
                if current_asset is not None:
                    current_asset.status = AssetStatus.FAILED
                    if "embedded_image_analysis_failed" not in current_asset.warning_codes:
                        current_asset.warning_codes.append("embedded_image_analysis_failed")
                parse_result.status = "partial"
                parse_result.warnings.append(
                    ParseWarning(
                        code="embedded_image_analysis_failed",
                        severity="warning",
                        unit_index=page_number,
                        asset_id=current_asset.asset_id if current_asset else None,
                    )
                )
                logger.warning(
                    "embedded image processing failed",
                    extra={"unit": page_number, "error_code": type(exc).__name__},
                )
        self._project_asset_descriptions(parse_result)

    @staticmethod
    def _project_asset_descriptions(parse_result: ContentParseResult) -> None:
        described = [
            asset
            for asset in parse_result.assets
            if asset.role not in {AssetRole.SOURCE, AssetRole.PREVIEW}
            and asset.visual_analysis is not None
        ]
        if not described:
            return
        markdown = [parse_result.renderings.markdown.rstrip(), "", "## 图片资产"]
        plain = [parse_result.renderings.plain_text.rstrip(), "", "图片资产："]
        for asset in described:
            analysis = asset.visual_analysis
            if analysis is None:
                continue
            description = analysis.detailed_description or analysis.summary
            markdown.extend(["", f"- `{asset.asset_id}`：{description}"])
            plain.append(f"- {asset.asset_id}：{description}")
            visible_text = analysis.visible_text.strip()
            if visible_text:
                indented = visible_text.replace("\n", "\n    ")
                markdown.append(f"  - 可见文本：{indented}")
                plain.append(f"  可见文本：{visible_text}")
        parse_result.renderings.markdown = "\n".join(markdown).strip()
        parse_result.renderings.plain_text = "\n".join(plain).strip()

    @staticmethod
    def _office_relationship_owner(relationship_name: str) -> str:
        path = PurePosixPath(relationship_name)
        parts = list(path.parts)
        index = parts.index("_rels")
        owner_name = parts[index + 1]
        if owner_name.endswith(".rels"):
            owner_name = owner_name[: -len(".rels")]
        return "/".join([*parts[:index], owner_name])

    def extract_office_images(self, path: Path, mime_type: str) -> list[EmbeddedImage]:
        """Read only explicit OOXML image relationships; all other media is ignored."""

        output: list[EmbeddedImage] = []
        total = 0
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            relationship_names = sorted(
                name
                for name in names
                if name.endswith(".rels")
                and (
                    (mime_type == DOCX_MIME and name.startswith("word/"))
                    or (mime_type == PPTX_MIME and name.startswith("ppt/slides/_rels/"))
                )
            )
            for relationship_name in relationship_names:
                owner = self._office_relationship_owner(relationship_name)
                match = re.search(r"slide(?P<index>\d+)\.xml$", owner)
                unit_index = int(match.group("index")) if match else 1
                root = ElementTree.fromstring(archive.read(relationship_name))
                owner_root = (
                    ElementTree.fromstring(archive.read(owner)) if owner in names else None
                )
                for relationship in root.findall(f"{{{_RELATIONSHIP_NS}}}Relationship"):
                    if not relationship.attrib.get("Type", "").endswith("/image"):
                        continue
                    target = relationship.attrib.get("Target", "")
                    package_path = posixpath.normpath(
                        posixpath.join(posixpath.dirname(owner), target)
                    )
                    if package_path not in names:
                        continue
                    info = archive.getinfo(package_path)
                    if info.file_size > self.max_asset_bytes:
                        raise ParserError("embedded image exceeds per-asset safety limit")
                    total += info.file_size
                    if total > self.max_extracted_bytes:
                        raise ParserError("embedded images exceed expanded-byte safety limit")
                    content = archive.read(info)
                    detected = detect_mime_type(content[:64], suffix=Path(package_path).suffix)
                    if detected is None or not detected.startswith("image/"):
                        continue
                    relationship_id = relationship.attrib.get("Id", "")
                    placements = (
                        sum(
                            value == relationship_id
                            for element in owner_root.iter()
                            for value in element.attrib.values()
                        )
                        if owner_root is not None
                        else 1
                    )
                    for placement_index in range(1, max(1, placements) + 1):
                        output.append(
                            EmbeddedImage(
                                content=content,
                                filename=PurePosixPath(package_path).name,
                                mime_type=detected,
                                unit_index=unit_index,
                                relationship_id=relationship_id,
                                placement_index=placement_index,
                            )
                        )
                        if len(output) > self.max_assets:
                            raise ParserError("content contains too many embedded images")
        return output

    async def parse_office(
        self,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        content_id: str,
        progress_callback: ProgressCallback | None,
        cancel_event: object | None,
        image_parser: DocumentParser | None = None,
    ) -> DocumentParseResult:
        if _cancelled(cancel_event):
            raise ParserCancelledError("job was cancelled before parsing")
        selected = list(range(1, source.page_count + 1))
        if options.unit_range:
            from app.utils.page_range import parse_page_range

            selected = parse_page_range(options.unit_range, source.page_count)

        def convert() -> object:
            from docling.datamodel.base_models import InputFormat
            from docling.document_converter import DocumentConverter

            input_format = InputFormat.DOCX if source.mime_type == DOCX_MIME else InputFormat.PPTX
            converter = DocumentConverter(allowed_formats=[input_format])
            return converter.convert(source.path)

        started = time.perf_counter()
        conversion = await asyncio.to_thread(convert)
        document = getattr(conversion, "document", None)
        if document is None:
            raise ParserError("Docling returned no Office document")
        page_results: list[PageParseResult] = []
        for current, unit_index in enumerate(selected, start=1):
            if _cancelled(cancel_event):
                raise ParserCancelledError("job was cancelled")
            if source.mime_type == PPTX_MIME:
                markdown = await asyncio.to_thread(document.export_to_markdown, page_no=unit_index)
                text = await asyncio.to_thread(document.export_to_text, page_no=unit_index)
            else:
                markdown = await asyncio.to_thread(document.export_to_markdown)
                text = await asyncio.to_thread(document.export_to_text)
            page_results.append(
                PageParseResult(
                    page_number=unit_index,
                    backend="docling-office",
                    content=str(markdown),
                    plain_text=str(text),
                    diagnostics=PageDiagnostics(source_kind=PageSourceKind.NATIVE),
                )
            )
            await _notify(progress_callback, current, len(selected), "unit.processed")
        markdown = "\n\n---\n\n".join(page.content or "" for page in page_results)
        plain_text = "\n\n".join(page.plain_text or "" for page in page_results)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        result = DocumentParseResult(
            document_id=content_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=len(page_results),
            markdown=markdown,
            plain_text=plain_text,
            pages=page_results,
            pipeline=ParsePipeline(profile=options.profile.value, primary="docling-office"),
            route_summary=RouteSummary(),
            quality_summary=QualitySummary(trusted_pages=len(page_results)),
            usage=ParseUsage(input_bytes=source.size_bytes, duration_ms=duration_ms),
        )
        kind = SourceKind.DOCX if source.mime_type == DOCX_MIME else SourceKind.PPTX
        unit_type = UnitType.DOCUMENT_BODY if kind == SourceKind.DOCX else UnitType.SLIDE
        units: list[ContentUnit] = []
        for page in page_results:
            unit_id = f"unit-{page.page_number}"
            text = page.plain_text or ""
            blocks: list[TextBlock] = []
            regions: list[ContentRegion] = []
            if text.strip():
                region_id = f"{unit_id}-body"
                block_id = f"{unit_id}-body-1"
                blocks.append(
                    TextBlock(
                        block_id=block_id,
                        unit_id=unit_id,
                        region_id=region_id,
                        reading_order=1,
                        text=text,
                        quality=ElementQuality.COMPLETE,
                        provenance=ElementProvenance(
                            selected_source=ProvenanceSourceKind.DOCLING,
                            supporting_sources=[ProvenanceSourceKind.OOXML],
                            sources=[
                                ProvenanceSource(
                                    source=ProvenanceSourceKind.DOCLING,
                                    backend="docling-office",
                                ),
                                ProvenanceSource(source=ProvenanceSourceKind.OOXML),
                            ],
                        ),
                    )
                )
                regions.append(
                    ContentRegion(
                        region_id=region_id,
                        unit_id=unit_id,
                        region_type=RegionType.TEXT,
                        reading_order=1,
                        block_ids=[block_id],
                    )
                )
            units.append(
                ContentUnit(
                    unit_id=unit_id,
                    unit_type=unit_type,
                    index=page.page_number,
                    status="completed",
                    regions=regions,
                    blocks=blocks,
                    renderings=ContentRenderings(
                        markdown=page.content or "", plain_text=text
                    ),
                    diagnostics=UnitDiagnostics(
                        source_kind="office",
                        quality_verdict="trusted",
                        selected_strategy="office_native",
                        native_text_characters=len(text),
                    ),
                    duration_ms=page.duration_ms,
                )
            )
        parse_result = ContentParseResult(
            source=ContentSource(
                content_id=content_id,
                source_sha256=_sha256_file(source.path),
                filename=source.filename,
                mime_type=source.mime_type,
                kind=kind,
                size_bytes=source.size_bytes,
                unit_count=source.page_count,
                slide_count=source.page_count if kind == SourceKind.PPTX else None,
            ),
            units=units,
            renderings=ContentRenderings(markdown=markdown, plain_text=plain_text),
            diagnostics=ContentQualitySummary(trusted_units=len(units)),
            runtime=ParseRuntime(
                profile=options.profile.value,
                primary_backend="docling-office",
                parser_version=str(setting(self.settings, "version", "0.4.0")),
                input_bytes=source.size_bytes,
                duration_ms=duration_ms,
            ),
            links=self._links(content_id),
        )
        for embedded in self.extract_office_images(source.path, source.mime_type):
            unit = next((item for item in units if item.index == embedded.unit_index), None)
            if unit is None:
                continue
            asset, _ = await self._persist_image_asset(
                content_id,
                embedded.content,
                filename=embedded.filename,
                role=AssetRole.EMBEDDED_IMAGE,
                location=AssetLocation(
                    unit_id=unit.unit_id,
                    slide_number=embedded.unit_index if kind == SourceKind.PPTX else None,
                    relationship_id=embedded.relationship_id,
                    placement_id=(
                        f"{embedded.relationship_id}:{embedded.placement_index}"
                    ),
                ),
            )
            asset = self._merge_asset(parse_result.assets, asset)
            if asset.asset_id not in unit.asset_ids:
                unit.asset_ids.append(asset.asset_id)
            if (
                asset.visual_analysis is None
                and "embedded_image_analysis_failed" not in asset.warning_codes
            ):
                try:
                    asset.visual_analysis = await self._analyze_embedded_image(
                        embedded.content,
                        asset,
                        options,
                        content_id=content_id,
                        image_parser=image_parser,
                        cancel_event=cancel_event,
                    )
                except ParserError as exc:
                    asset.status = AssetStatus.FAILED
                    asset.warning_codes.append("embedded_image_analysis_failed")
                    parse_result.status = "partial"
                    parse_result.warnings.append(
                        ParseWarning(
                            code="embedded_image_analysis_failed",
                            severity="warning",
                            unit_index=embedded.unit_index,
                            asset_id=asset.asset_id,
                        )
                    )
                    logger.warning(
                        "Office embedded image analysis failed",
                        extra={"unit": embedded.unit_index, "error_code": type(exc).__name__},
                    )
            try:
                preview = await self._preview_asset(
                    content_id,
                    asset,
                    embedded.content,
                    AssetLocation(
                        unit_id=unit.unit_id,
                        slide_number=embedded.unit_index if kind == SourceKind.PPTX else None,
                        relationship_id=embedded.relationship_id,
                        placement_id=f"{embedded.relationship_id}:{embedded.placement_index}",
                    ),
                )
            except ParserError as exc:
                preview = None
                if asset.status == AssetStatus.READY:
                    asset.status = AssetStatus.PARTIAL
                asset.warning_codes.append("embedded_image_preview_failed")
                parse_result.status = "partial"
                parse_result.warnings.append(
                    ParseWarning(
                        code="embedded_image_preview_failed",
                        severity="warning",
                        unit_index=embedded.unit_index,
                        asset_id=asset.asset_id,
                    )
                )
                logger.warning(
                    "Office embedded image preview failed",
                    extra={"unit": embedded.unit_index, "error_code": type(exc).__name__},
                )
            if preview is not None:
                preview.visual_analysis = asset.visual_analysis
                preview.status = asset.status
                preview.warning_codes = list(asset.warning_codes)
                preview = self._merge_asset(parse_result.assets, preview)
                if preview.asset_id not in unit.asset_ids:
                    unit.asset_ids.append(preview.asset_id)
        self._project_asset_descriptions(parse_result)
        result.markdown = parse_result.renderings.markdown
        result.plain_text = parse_result.renderings.plain_text
        if not options.include_renderings:
            parse_result.renderings = ContentRenderings()
            for unit in parse_result.units:
                unit.renderings = ContentRenderings()
            result.markdown = ""
            result.plain_text = ""
        result.parse_result = parse_result
        return result

    async def _run_process(
        self, *command: str, cancel_event: object | None = None
    ) -> tuple[bytes, bytes]:
        if not command or shutil.which(command[0]) is None:
            executable = command[0] if command else "media tool"
            raise ParserUnavailableError(f"{executable} is required for video analysis")
        if Path(command[0]).name == "ffmpeg":
            command = (
                command[0],
                "-threads",
                str(self.ffmpeg_threads),
                "-filter_threads",
                str(self.ffmpeg_threads),
                "-max_alloc",
                str(self.ffmpeg_max_alloc_bytes),
                *command[1:],
            )
        async with self._ffmpeg_semaphore:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication = asyncio.create_task(process.communicate())
            deadline = time.monotonic() + self.ffmpeg_timeout
            try:
                while True:
                    if _cancelled(cancel_event):
                        process.kill()
                        await communication
                        raise ParserCancelledError("job was cancelled during video decoding")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        process.kill()
                        await communication
                        raise ParserError("FFmpeg decoding timed out")
                    try:
                        stdout, stderr = await asyncio.wait_for(
                            asyncio.shield(communication), timeout=min(0.25, remaining)
                        )
                        break
                    except TimeoutError:
                        continue
            except asyncio.CancelledError:
                process.kill()
                await asyncio.gather(communication, return_exceptions=True)
                raise
        if process.returncode != 0:
            raise ParserError(
                "FFmpeg media processing failed",
                details={"return_code": int(process.returncode or -1)},
            )
        return stdout, stderr

    async def _video_metadata(
        self, path: Path, cancel_event: object | None = None
    ) -> VideoMetadata:
        stdout, _ = await self._run_process(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(path),
            cancel_event=cancel_event,
        )
        try:
            payload = json.loads(stdout)
            stream = payload["streams"][0]
            duration_ms = round(float(payload["format"]["duration"]) * 1000)
            numerator, denominator = str(stream.get("avg_frame_rate", "0/1")).split("/", 1)
            frame_rate = float(numerator) / float(denominator) if float(denominator) else None
            return VideoMetadata(
                duration_ms=duration_ms,
                width=int(stream["width"]),
                height=int(stream["height"]),
                codec=str(stream.get("codec_name") or "") or None,
                frame_rate=frame_rate if frame_rate and frame_rate > 0 else None,
            )
        except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise ParserError("ffprobe returned incomplete video metadata") from exc

    async def _scene_candidates(
        self, path: Path, cancel_event: object | None = None
    ) -> list[tuple[float, float]]:
        _, stderr = await self._run_process(
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"select='gt(scene,{self.scene_threshold})',metadata=print",
            "-f",
            "null",
            "-",
            cancel_event=cancel_event,
        )
        lines = stderr.decode("utf-8", errors="replace").splitlines()
        output: list[tuple[float, float]] = []
        current_time: float | None = None
        for line in lines:
            if match := _SCENE_TIME_RE.search(line):
                current_time = float(match.group("time"))
            if current_time is not None and (match := _SCENE_SCORE_RE.search(line)):
                output.append((current_time, float(match.group("score"))))
                current_time = None
        return output

    def _select_timestamps(
        self,
        duration_seconds: float,
        candidates: list[tuple[float, float]],
        *,
        frame_rate: float | None = None,
    ) -> list[float]:
        # Container durations commonly point just past the final presentation
        # timestamp. Keep the tail sample on a decodable frame, especially for
        # low-frame-rate and variable-frame-rate inputs.
        tail_margin = (
            min(max(0.05, 1.25 / frame_rate), duration_seconds / 2)
            if frame_rate
            else min(0.05, duration_seconds / 100)
        )
        end = max(0.0, duration_seconds - tail_margin)
        scored = list(candidates)
        available = max(0, self.max_keyframes - (2 if end > 0 else 1))
        if len(scored) > available and available > 0:
            bucket_width = max(duration_seconds / self.max_keyframes, 0.001)
            buckets: dict[int, tuple[float, float]] = {}
            for timestamp, score in scored:
                bucket = min(self.max_keyframes - 1, int(timestamp / bucket_width))
                previous = buckets.get(bucket)
                if (
                    previous is None
                    or score > previous[1]
                    or (score == previous[1] and timestamp < previous[0])
                ):
                    buckets[bucket] = (timestamp, score)
            scored = list(buckets.values())
        target = min(self.max_keyframes, max(3, math.ceil(duration_seconds / 10) + 2))
        if target > 2:
            scored.extend(
                (duration_seconds * index / (target - 1), 0.0) for index in range(1, target - 1)
            )
        ordered = sorted(scored, key=lambda item: (-item[1], item[0]))
        selected: list[tuple[float, float]] = [(0.0, 2.0)]
        if end > 0:
            selected.append((end, 2.0))
        for timestamp, score in ordered:
            if len(selected) >= self.max_keyframes:
                break
            timestamp = min(end, max(0.0, timestamp))
            if any(abs(timestamp - known) < self.min_spacing for known, _ in selected):
                continue
            selected.append((timestamp, score))
            if len(selected) >= self.max_keyframes or len(selected) >= available + 2:
                break
        return sorted(timestamp for timestamp, _ in selected)

    async def _extract_frame(
        self, path: Path, timestamp: float, cancel_event: object | None = None
    ) -> bytes:
        stdout, _ = await self._run_process(
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(path),
            "-an",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1920,iw)':-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
            cancel_event=cancel_event,
        )
        if not stdout:
            raise ParserError("FFmpeg returned an empty keyframe")
        return stdout

    async def parse_video(
        self,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        content_id: str,
        progress_callback: ProgressCallback | None,
        cancel_event: object | None,
    ) -> DocumentParseResult:
        if options.unit_range:
            raise ParserError("video inputs do not accept unit_range")
        if not self.vlm.enabled:
            raise ParserUnavailableError("VLM is required for standalone video analysis")
        started = time.perf_counter()
        metadata = await self._video_metadata(source.path, cancel_event)
        candidates = await self._scene_candidates(source.path, cancel_event)
        timestamps = self._select_timestamps(
            metadata.duration_ms / 1000,
            candidates,
            frame_rate=metadata.frame_rate,
        )
        video_bytes = await asyncio.to_thread(source.path.read_bytes)
        video_digest = _sha256_bytes(video_bytes)
        source_asset_id = _asset_id(video_digest)
        await self.storage.write_asset(
            content_id, source_asset_id, source.filename, video_bytes, derived=False
        )
        source_asset = ContentAsset(
            asset_id=source_asset_id,
            kind=AssetKind.VIDEO,
            role=AssetRole.SOURCE,
            mime_type=source.mime_type,
            sha256=video_digest,
            size_bytes=source.size_bytes,
            filename=source.filename,
            width=metadata.width,
            height=metadata.height,
            duration_ms=metadata.duration_ms,
            locations=[AssetLocation(unit_id="unit-1")],
            download_url=f"/v1/content/jobs/{content_id}/assets/{source_asset_id}",
        )
        assets = [source_asset]
        keyframes: list[VideoKeyframe] = []
        qwen_duration = 0
        visual_calls = 0
        for index, timestamp in enumerate(timestamps, start=1):
            if _cancelled(cancel_event):
                raise ParserCancelledError("job was cancelled")
            frame = await self._extract_frame(source.path, timestamp, cancel_event)
            asset, _ = await self._persist_image_asset(
                content_id,
                frame,
                filename=f"keyframe-{index:03d}-{round(timestamp * 1000)}ms.png",
                role=AssetRole.KEYFRAME,
                location=AssetLocation(unit_id="unit-1", timestamp_ms=round(timestamp * 1000)),
                parent_asset_id=source_asset_id,
                derived=True,
            )
            existing = next((item for item in assets if item.sha256 == asset.sha256), None)
            if existing is None:
                assets.append(asset)
            else:
                asset = self._merge_asset(assets, asset)
            if asset.visual_analysis is None:
                visual_started = time.perf_counter()
                asset.visual_analysis = await self._describe(
                    frame, language=options.description_language
                )
                visual_calls += 1
                qwen_duration += max(
                    0, round((time.perf_counter() - visual_started) * 1000)
                )
            keyframes.append(
                VideoKeyframe(
                    asset_id=asset.asset_id,
                    timestamp_ms=round(timestamp * 1000),
                    visual_analysis=asset.visual_analysis,
                )
            )
            await _notify(progress_callback, index, len(timestamps), "frame.processed")
        descriptions = "\n".join(
            f"[{frame.timestamp_ms / 1000:.3f}s] {frame.visual_analysis.summary}"
            for frame in keyframes
        )
        representative_images = [
            await asyncio.to_thread(
                self.storage.asset_path(content_id, frame.asset_id).read_bytes  # type: ignore[union-attr]
            )
            for frame in keyframes[: min(8, len(keyframes))]
        ]
        summary_language = self._language_instruction(options.description_language)
        summary_completion = await self.vlm.complete_structured(
            representative_images,
            "Summarize this video using only the following sampled-frame observations. "
            "Do not claim that unsampled events were observed. Include that limitation. "
            f"{summary_language}\n"
            f"{descriptions}",
            _VideoSummaryResponse,
            max_tokens=int(setting(self.settings, "video_summary_max_tokens", 4096)),
            reasoning_effort=str(
                setting(self.settings, "video_summary_reasoning_effort", "none")
            ),
        )
        qwen_duration += summary_completion.duration_ms
        qwen_calls = visual_calls + 1
        scenes: list[VideoScene] = []
        for index, keyframe in enumerate(keyframes):
            previous = keyframes[index - 1].timestamp_ms if index else 0
            following = (
                keyframes[index + 1].timestamp_ms
                if index + 1 < len(keyframes)
                else metadata.duration_ms
            )
            start_ms = 0 if index == 0 else (previous + keyframe.timestamp_ms) // 2
            end_ms = (
                metadata.duration_ms
                if index + 1 == len(keyframes)
                else (keyframe.timestamp_ms + following) // 2
            )
            scene_id = f"scene-{index + 1}"
            keyframe.scene_id = scene_id
            scenes.append(
                VideoScene(
                    scene_id=scene_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    keyframe_asset_ids=[keyframe.asset_id],
                )
            )
        timeline_markdown = "\n".join(
            f"- `{frame.timestamp_ms / 1000:.3f}s`：{frame.visual_analysis.summary}"
            for frame in keyframes
        )
        markdown = (
            f"# 视频摘要\n\n{summary_completion.value.summary}\n\n"
            f"## 采样关键帧\n\n{timeline_markdown}"
        )
        plain_text = f"视频摘要：{summary_completion.value.summary}\n\n采样关键帧：\n" + "\n".join(
            f"{frame.timestamp_ms / 1000:.3f}s：{frame.visual_analysis.summary}"
            for frame in keyframes
        )
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        video_analysis = VideoAnalysis(
            duration_ms=metadata.duration_ms,
            width=metadata.width,
            height=metadata.height,
            codec=metadata.codec,
            frame_rate=metadata.frame_rate,
            summary=summary_completion.value.summary,
            scenes=scenes,
            keyframes=keyframes,
        )
        parse_result = ContentParseResult(
            source=ContentSource(
                content_id=content_id,
                source_sha256=video_digest,
                filename=source.filename,
                mime_type=source.mime_type,
                kind=SourceKind.VIDEO,
                size_bytes=source.size_bytes,
                unit_count=1,
                duration_ms=metadata.duration_ms,
                width=metadata.width,
                height=metadata.height,
            ),
            units=[
                ContentUnit(
                    unit_id="unit-1",
                    unit_type=UnitType.VIDEO,
                    index=1,
                    status="completed",
                    width=metadata.width,
                    height=metadata.height,
                    asset_ids=list(dict.fromkeys(asset.asset_id for asset in assets)),
                    renderings=ContentRenderings(markdown=markdown, plain_text=plain_text),
                    diagnostics=UnitDiagnostics(
                        source_kind="video",
                        quality_verdict="trusted",
                        selected_strategy="video_keyframes",
                        qwen_calls=qwen_calls,
                        qwen_duration_ms=qwen_duration,
                    ),
                    duration_ms=duration_ms,
                )
            ],
            assets=assets,
            video_analysis=video_analysis,
            renderings=ContentRenderings(
                markdown=markdown if options.include_renderings else "",
                plain_text=plain_text if options.include_renderings else "",
            ),
            diagnostics=ContentQualitySummary(trusted_units=1, visual_units=1),
            runtime=ParseRuntime(
                profile=options.profile.value,
                primary_backend="ffmpeg+vlm",
                visual_backend=self.vlm.model,
                parser_version=str(setting(self.settings, "version", "0.4.0")),
                input_bytes=source.size_bytes,
                duration_ms=duration_ms,
                qwen_calls=qwen_calls,
                ffmpeg_duration_ms=max(0, duration_ms - qwen_duration),
            ),
            links=self._links(content_id),
        )
        result = DocumentParseResult(
            document_id=content_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=1,
            processed_pages=1,
            markdown=markdown if options.include_renderings else "",
            plain_text=plain_text if options.include_renderings else "",
            pages=[
                PageParseResult(
                    page_number=1,
                    backend="ffmpeg+vlm",
                    content=markdown if options.include_renderings else "",
                    plain_text=plain_text if options.include_renderings else "",
                    diagnostics=PageDiagnostics(source_kind=PageSourceKind.MIXED),
                    duration_ms=duration_ms,
                )
            ],
            pipeline=ParsePipeline(
                profile=options.profile.value, primary="ffmpeg", vlm=self.vlm.name
            ),
            route_summary=RouteSummary(vlm_pages=1),
            quality_summary=QualitySummary(
                trusted_pages=1, visual_pages=1, qwen_calls=qwen_calls
            ),
            usage=ParseUsage(input_bytes=source.size_bytes, duration_ms=duration_ms),
            parse_result=parse_result,
        )
        return result
