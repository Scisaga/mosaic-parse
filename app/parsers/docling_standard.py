"""Stable adapter around Docling's standard PDF/image pipeline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from app.models.backend import BackendState, BackendStatus
from app.models.parse_options import ContentParseOptions, ParseProfile
from app.models.parse_result import (
    DocumentParseResult,
    PageParseResult,
    PageStatus,
    ParsePipeline,
    ParseUsage,
    ParseWarning,
    RouteSummary,
    WarningSeverity,
)
from app.models.source import StoredSource
from app.parsers.base import (
    DocumentParser,
    ParserCancelledError,
    ParserError,
    ParserUnavailableError,
    ProgressCallback,
)
from app.parsers.glm_ocr_remote import GlmOcrRemoteAdapter
from app.services.table_service import export_page_markdown, extract_table_fragments
from app.utils.page_range import group_consecutive_pages, parse_page_range
from app.utils.settings import setting

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PictureCandidate:
    """Runtime-only pointer to a picture detected in Docling reading order."""

    page_number: int
    placeholder_index: int
    caption: str
    normalized_bbox: tuple[float, float, float, float]


class _ObservableStandardPdfPipelineMixin:
    """Expose Docling's real page-pipeline boundary to the async adapter.

    Docling 2.119 has no public page callback, but its threaded standard
    pipeline invokes ``_release_page_resources`` exactly once when a page has
    passed preprocessing, layout, OCR, table recognition and page assembly.
    Keep this tiny, version-guarded bridge at the dependency boundary; the
    final ``page.completed`` event is still emitted only after Docling returns
    and that page can be exported from the assembled document.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._mosaicparse_progress_lock = threading.Lock()
        self._mosaicparse_progress_sink: queue.Queue[int] | None = None

    def set_page_progress_sink(self, sink: queue.Queue[int] | None) -> None:
        with self._mosaicparse_progress_lock:
            self._mosaicparse_progress_sink = sink

    def _release_page_resources(self, item: object) -> None:
        super()._release_page_resources(item)  # type: ignore[misc]
        page_number = getattr(item, "page_no", None)
        with self._mosaicparse_progress_lock:
            sink = self._mosaicparse_progress_sink
        if sink is not None and isinstance(page_number, int) and page_number > 0:
            sink.put(page_number)


def _observable_standard_pipeline_class() -> type:
    """Build the pinned-Docling subclass lazily so API-only imports stay cheap."""

    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    class ObservableStandardPdfPipeline(
        _ObservableStandardPdfPipelineMixin,
        StandardPdfPipeline,
    ):
        pass

    return ObservableStandardPdfPipeline


class DoclingStandardParser(DocumentParser):
    name = "docling-standard"

    def __init__(self, settings: object | None, glm_adapter: GlmOcrRemoteAdapter) -> None:
        self.settings = settings
        self.glm_adapter = glm_adapter
        self._worker_count = int(setting(settings, "parser_workers", 1))
        self._available_slots: asyncio.Queue[int] = asyncio.Queue(maxsize=self._worker_count)
        for slot in range(self._worker_count):
            self._available_slots.put_nowait(slot)
        self._converters: OrderedDict[tuple[int, str, bool, tuple[str, ...], bool], object] = (
            OrderedDict()
        )
        self._inflight_conversions: set[asyncio.Task[object]] = set()
        self._dependency_error: str | None = None
        self._initialized = False

    @staticmethod
    def _cancelled(cancel_event: object | None) -> bool:
        return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())

    async def initialize(self) -> None:
        try:
            # Import only during application startup; importing this module itself
            # remains cheap and works in API-only/test environments.
            await asyncio.to_thread(
                __import__, "docling.document_converter", fromlist=["DocumentConverter"]
            )
            glm_status = await self.glm_adapter.probe()
            languages = list(getattr(self.settings, "default_languages", None) or ["zh", "en"])
            # Build and initialize every configured worker's balanced pipeline.
            # This loads/verifies model artifacts before /ready can report true.
            for worker_slot in range(self._worker_count):
                await asyncio.to_thread(
                    self._initialize_converter_slot,
                    worker_slot,
                    languages,
                    glm_status.ready,
                )
            self._dependency_error = None
            self._initialized = True
        except Exception as exc:
            self._dependency_error = f"{type(exc).__name__}: {exc}"
            self._initialized = False
            logger.exception("failed to initialize Docling standard parser")

    async def probe(self, *, force: bool = False) -> BackendStatus:
        if force or not self._initialized:
            await self.initialize()
        if self._initialized:
            return BackendStatus(
                name=self.name,
                state=BackendState.READY,
                detail=f"{len(self._converters)} converter profile(s) cached",
            )
        return BackendStatus(
            name=self.name,
            state=BackendState.UNAVAILABLE,
            detail=self._dependency_error or "Docling is not initialized",
        )

    def _get_converter(
        self,
        worker_slot: int,
        profile: ParseProfile,
        force_full_page_ocr: bool,
        languages: list[str],
        glm_ready: bool,
    ) -> object:
        # A converter is never shared by concurrently running parser slots; upstream
        # does not promise that DocumentConverter is thread-safe.
        key = (worker_slot, profile.value, force_full_page_ocr, tuple(languages), glm_ready)
        if key in self._converters:
            self._converters.move_to_end(key)
            return self._converters[key]
        converter = self._build_converter(profile, force_full_page_ocr, languages, glm_ready)
        self._converters[key] = converter
        self._converters.move_to_end(key)
        while len(self._converters) > max(16, self._worker_count * 4):
            self._converters.popitem(last=False)
        return converter

    def _initialize_converter_slot(
        self,
        worker_slot: int,
        languages: list[str],
        glm_ready: bool,
    ) -> None:
        from docling.datamodel.base_models import InputFormat

        converter = self._get_converter(
            worker_slot,
            ParseProfile.BALANCED,
            False,
            languages,
            glm_ready,
        )
        converter.initialize_pipeline(InputFormat.PDF)  # type: ignore[attr-defined]

    def _build_converter(
        self,
        profile: ParseProfile,
        force_full_page_ocr: bool,
        languages: list[str],
        glm_ready: bool,
    ) -> object:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, ImageFormatOption, PdfFormatOption

        configured_artifacts = Path(
            setting(self.settings, "docling_artifacts_path", "/models/docling")
        )
        allow_download = bool(setting(self.settings, "docling_model_download", True))
        if not allow_download and not configured_artifacts.is_dir():
            raise FileNotFoundError(
                f"Docling artifacts path does not exist: {configured_artifacts}"
            )
        has_prefetched_artifacts = configured_artifacts.is_dir() and any(
            configured_artifacts.iterdir()
        )
        artifacts_path: Path | None = (
            configured_artifacts if has_prefetched_artifacts or not allow_download else None
        )
        if artifacts_path is None:
            # Docling itself reads DOCLING_ARTIFACTS_PATH into a process-global
            # setting. If that variable names a fresh/empty Compose volume it
            # suppresses Hugging Face downloads even though this service allows
            # them. Our explicit auto-download decision must win here.
            from docling.datamodel.settings import settings as docling_settings

            docling_settings.artifacts_path = None

        scale = float(setting(self.settings, "glm_ocr_scale", 3.0))
        if profile == ParseProfile.FAST:
            scale = min(scale, 2.0)
        elif profile == ParseProfile.ACCURATE:
            scale = max(scale, 3.0)
        configured_table_mode = str(setting(self.settings, "docling_table_mode", "accurate"))
        table_mode = (
            TableFormerMode.FAST
            if profile == ParseProfile.FAST or configured_table_mode == "fast"
            else TableFormerMode.ACCURATE
        )
        device = str(setting(self.settings, "docling_device", "cpu"))
        if device.startswith("cuda"):
            import torch

            requested_index = int(device.partition(":")[2] or 0)
            if not torch.cuda.is_available() or requested_index >= torch.cuda.device_count():
                raise RuntimeError(f"configured Docling device {device!r} is unavailable")
        pipeline_options = PdfPipelineOptions(
            do_ocr=bool(glm_ready),
            do_table_structure=True,
            allow_external_plugins=bool(glm_ready),
            enable_remote_services=bool(glm_ready),
            artifacts_path=artifacts_path,
            document_timeout=float(setting(self.settings, "content_timeout_seconds", 900)),
            accelerator_options=AcceleratorOptions(device=device),
        )
        # torch.compile has a very large first-document cost on the default CPU
        # deployment. It remains opt-in for sustained-throughput installations.
        layout_engine_options = cast(Any, pipeline_options.layout_options).engine_options
        if hasattr(layout_engine_options, "compile_model"):
            layout_engine_options.compile_model = bool(
                setting(self.settings, "docling_compile_models", False)
            )
        cast(TableStructureOptions, pipeline_options.table_structure_options).mode = table_mode
        cast(
            TableStructureOptions, pipeline_options.table_structure_options
        ).do_cell_matching = bool(setting(self.settings, "docling_do_cell_matching", True))
        if hasattr(pipeline_options, "force_backend_text"):
            pipeline_options.force_backend_text = bool(
                setting(self.settings, "docling_force_backend_text", False)
            )
        if glm_ready:
            pipeline_options.ocr_options = self.glm_adapter.build_options(
                languages=languages,
                scale=scale,
                force_full_page_ocr=force_full_page_ocr,
            )
        pipeline_class = _observable_standard_pipeline_class()
        pdf_format_option = PdfFormatOption(
            pipeline_options=pipeline_options,
            pipeline_cls=pipeline_class,
        )
        image_format_option = ImageFormatOption(
            pipeline_options=pipeline_options,
            pipeline_cls=pipeline_class,
        )
        return DocumentConverter(
            allowed_formats=[InputFormat.PDF, InputFormat.IMAGE],
            format_options={
                InputFormat.PDF: pdf_format_option,
                InputFormat.IMAGE: image_format_option,
            },
        )

    @staticmethod
    async def _notify(
        callback: ProgressCallback | None, current: int, total: int, state: str
    ) -> None:
        if callback is None:
            return
        result = callback(current, total, state)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _conversion_error_messages(conversion: object) -> list[str]:
        messages: list[str] = []
        for error in getattr(conversion, "errors", []) or []:
            text = (
                getattr(error, "error_message", None)
                or getattr(error, "message", None)
                or str(error)
            )
            if text:
                messages.append(str(text)[:1_000])
        return messages

    @staticmethod
    def _overlapping_text_duplicate_counts(
        document: object,
        page_group: tuple[int, int],
    ) -> dict[int, Counter[str]]:
        """Count only identical text items whose provenance boxes overlap almost exactly."""

        iterator = getattr(document, "iterate_items", None)
        if not callable(iterator):
            return {}
        seen: dict[tuple[int, str], list[tuple[float, float, float, float]]] = {}
        duplicates: dict[int, Counter[str]] = {}
        for item, _level in iterator():
            text = re.sub(r"\s+", " ", str(getattr(item, "text", "") or "")).strip()
            provenance = getattr(item, "prov", None) or []
            if not text or not provenance:
                continue
            prov = provenance[0]
            page_number = getattr(prov, "page_no", None)
            if (
                not isinstance(page_number, int)
                or not page_group[0] <= page_number <= page_group[1]
            ):
                continue
            box = getattr(prov, "bbox", None)
            if box is None:
                continue
            left = float(getattr(box, "l", 0.0))
            right = float(getattr(box, "r", 0.0))
            top = float(getattr(box, "t", 0.0))
            bottom = float(getattr(box, "b", 0.0))
            bbox = (min(left, right), min(top, bottom), max(left, right), max(top, bottom))
            area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
            if area <= 0:
                continue
            key = (page_number, text)
            is_duplicate = False
            for previous in seen.get(key, []):
                intersection = max(
                    0.0, min(bbox[2], previous[2]) - max(bbox[0], previous[0])
                ) * max(0.0, min(bbox[3], previous[3]) - max(bbox[1], previous[1]))
                previous_area = max(0.0, previous[2] - previous[0]) * max(
                    0.0, previous[3] - previous[1]
                )
                union = area + previous_area - intersection
                if union > 0 and intersection / union >= 0.80:
                    is_duplicate = True
                    break
            seen.setdefault(key, []).append(bbox)
            if is_duplicate:
                duplicates.setdefault(page_number, Counter())[text] += 1
        return duplicates

    @staticmethod
    def _remove_overlapping_text_duplicates(markdown: str, duplicates: Counter[str]) -> str:
        if not duplicates:
            return markdown
        remaining = duplicates.copy()
        seen: Counter[str] = Counter()
        output: list[str] = []
        for line in markdown.splitlines():
            normalized = re.sub(r"\s+", " ", re.sub(r"^\s*(?:#{1,6}|[-+*])\s+", "", line)).strip()
            if normalized in remaining:
                seen[normalized] += 1
                if seen[normalized] > 1 and remaining[normalized] > 0:
                    remaining[normalized] -= 1
                    continue
            output.append(line)
        return "\n".join(output)

    @staticmethod
    def _extract_picture_candidates(
        document: object,
        page_group: tuple[int, int],
    ) -> list[PictureCandidate]:
        """Retain only geometry, never Docling images or document objects.

        Keeping ``generate_picture_images`` enabled makes the threaded PDF
        pipeline retain rendered pages until document assembly, which is
        costly for long inputs.  Provenance already provides a stable crop in
        page coordinates, so downstream VLM enrichment can render that crop
        directly from the source PDF instead.
        """

        iterator = getattr(document, "iterate_items", None)
        pages = getattr(document, "pages", None)
        if not callable(iterator) or not isinstance(pages, dict):
            return []
        try:
            from docling_core.types.doc import PictureItem
        except ImportError:
            return []

        candidates: list[PictureCandidate] = []
        ordinals: dict[int, int] = {}
        for item, _level in iterator():
            if not isinstance(item, PictureItem) or not item.prov:
                continue
            provenance = item.prov[0]
            page_number = provenance.page_no
            if not isinstance(page_number, int) or not (
                page_group[0] <= page_number <= page_group[1]
            ):
                continue
            placeholder_index = ordinals.get(page_number, 0)
            ordinals[page_number] = placeholder_index + 1
            page = pages.get(page_number)
            size = getattr(page, "size", None)
            width = float(getattr(size, "width", 0.0) or 0.0)
            height = float(getattr(size, "height", 0.0) or 0.0)
            if width <= 0 or height <= 0:
                continue
            try:
                bbox = provenance.bbox.to_top_left_origin(page_height=height)
                left = max(0.0, min(1.0, float(bbox.l) / width))
                top = max(0.0, min(1.0, float(bbox.t) / height))
                right = max(0.0, min(1.0, float(bbox.r) / width))
                bottom = max(0.0, min(1.0, float(bbox.b) / height))
                caption = str(item.caption_text(cast(Any, document)) or "").strip()
            except (AttributeError, TypeError, ValueError):
                continue
            if right <= left or bottom <= top:
                continue
            candidates.append(
                PictureCandidate(
                    page_number=page_number,
                    placeholder_index=placeholder_index,
                    caption=caption,
                    normalized_bbox=(left, top, right, bottom),
                )
            )
        return candidates

    def _convert(
        self,
        converter: object,
        source: StoredSource,
        page_group: tuple[int, int],
    ) -> object:
        kwargs: dict[str, object] = {
            "source": source.path,
            "max_num_pages": source.page_count,
            "max_file_size": int(setting(self.settings, "max_upload_bytes", 200 * 1024 * 1024)),
        }
        # Always pass the exact contiguous group. In particular, a sparse
        # selection such as 1,1000 must never expand into 1..1000.
        kwargs["page_range"] = page_group
        return converter.convert(**kwargs)  # type: ignore[attr-defined]

    def _convert_in_slot(
        self,
        worker_slot: int,
        source: StoredSource,
        page_group: tuple[int, int],
        options: ContentParseOptions,
        force_ocr: bool,
        glm_ready: bool,
        progress_sink: queue.Queue[int] | None = None,
    ) -> object:
        converter = self._get_converter(
            worker_slot,
            options.profile,
            force_ocr,
            options.language,
            glm_ready,
        )
        pipeline = self._converter_pipeline(converter, source.mime_type)
        if progress_sink is not None:
            self._set_pipeline_progress_sink(pipeline, progress_sink)
        try:
            return self._convert(converter, source, page_group)
        finally:
            if progress_sink is not None:
                self._set_pipeline_progress_sink(pipeline, None)

    @staticmethod
    def _converter_pipeline(converter: object, mime_type: str) -> object | None:
        """Return the already-cached standard pipeline for this source format."""

        try:
            from docling.datamodel.base_models import InputFormat

            input_format = InputFormat.PDF if mime_type == "application/pdf" else InputFormat.IMAGE
            return converter._get_pipeline(input_format)  # type: ignore[attr-defined]
        except (AttributeError, ImportError):
            return None

    @staticmethod
    def _set_pipeline_progress_sink(pipeline: object | None, sink: queue.Queue[int] | None) -> None:
        setter = getattr(pipeline, "set_page_progress_sink", None)
        if callable(setter):
            setter(sink)

    @staticmethod
    async def _drain_pipeline_progress(
        conversion_task: asyncio.Task[object],
        progress_sink: queue.Queue[int],
        callback: ProgressCallback | None,
        *,
        completed_before_group: int,
        total: int,
        selected_pages: set[int],
        announced_pages: set[int],
    ) -> None:
        """Forward real Docling page-boundary events while conversion is running."""

        while not conversion_task.done() or not progress_sink.empty():
            try:
                page_number = progress_sink.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.025)
                continue
            if page_number not in selected_pages or page_number in announced_pages:
                continue
            announced_pages.add(page_number)
            current = min(total, completed_before_group + len(announced_pages))
            await DoclingStandardParser._notify(callback, current, total, "page.processed")

    async def parse(
        self,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        document_id: str,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult:
        if not self._initialized:
            await self.initialize()
        if not self._initialized:
            raise ParserUnavailableError(self._dependency_error or "Docling is unavailable")
        if self._cancelled(cancel_event):
            raise ParserCancelledError("job was cancelled before parsing")
        pages = parse_page_range(options.page_range, source.page_count)
        page_groups = group_consecutive_pages(pages)
        glm_status = await self.glm_adapter.probe()
        force_ocr = False
        started = time.perf_counter()
        await self._notify(progress_callback, 0, len(pages), "document.started")
        worker_slot = await self._available_slots.get()
        release_slot = True
        parsed_pages: list[PageParseResult] = []
        picture_candidates: list[PictureCandidate] = []
        table_fragments: list[object] = []
        completed_count = 0
        page_backend = self.glm_adapter.name if force_ocr else self.name
        try:
            for page_group in page_groups:
                if self._cancelled(cancel_event):
                    raise ParserCancelledError("job was cancelled")
                progress_sink: queue.Queue[int] = queue.Queue()
                announced_pages: set[int] = set()
                selected_group_pages = set(range(page_group[0], page_group[1] + 1))
                conversion_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._convert_in_slot,
                        worker_slot,
                        source,
                        page_group,
                        options,
                        force_ocr,
                        glm_status.ready,
                        progress_sink,
                    )
                )
                self._inflight_conversions.add(conversion_task)
                conversion_task.add_done_callback(self._inflight_conversions.discard)
                progress_task = asyncio.create_task(
                    self._drain_pipeline_progress(
                        conversion_task,
                        progress_sink,
                        progress_callback,
                        completed_before_group=completed_count,
                        total=len(pages),
                        selected_pages=selected_group_pages,
                        announced_pages=announced_pages,
                    )
                )
                try:
                    conversion = await asyncio.shield(conversion_task)
                except asyncio.CancelledError:
                    # to_thread cannot be stopped. Keep this converter slot reserved
                    # until the upstream conversion really exits, preventing unsafe
                    # reuse after an API timeout or worker cancellation.
                    release_slot = False

                    def release_when_done(_task: asyncio.Task[object]) -> None:
                        self._available_slots.put_nowait(worker_slot)

                    conversion_task.add_done_callback(release_when_done)
                    raise
                finally:
                    if conversion_task.done():
                        await progress_task
                    else:
                        # The shielded Docling thread must keep its converter
                        # slot, but the request-scoped progress forwarder must
                        # stop immediately so a timed-out/cancelled job cannot
                        # emit events after its terminal state.
                        progress_task.cancel()
                        await asyncio.gather(progress_task, return_exceptions=True)

                status_text = str(getattr(conversion, "status", "success")).lower()
                if status_text.endswith("failure") or status_text.endswith("skipped"):
                    messages = self._conversion_error_messages(conversion)
                    raise ParserError(
                        "Docling reported conversion failure", details={"errors": messages}
                    )
                document = getattr(conversion, "document", None)
                if document is None:
                    raise ParserError("Docling returned no document")
                error_messages = self._conversion_error_messages(conversion)
                picture_candidates.extend(self._extract_picture_candidates(document, page_group))
                group_table_fragments = extract_table_fragments(document, page_group)
                table_fragments.extend(group_table_fragments)
                overlapping_duplicates = self._overlapping_text_duplicate_counts(
                    document, page_group
                )

                for page_number in range(page_group[0], page_group[1] + 1):
                    if self._cancelled(cancel_event):
                        raise ParserCancelledError("job was cancelled")
                    page_started = time.perf_counter()
                    try:
                        markdown = await asyncio.to_thread(
                            export_page_markdown,
                            document,
                            page_number,
                            group_table_fragments,
                        )
                        duplicate_counts = overlapping_duplicates.get(page_number, Counter())
                        markdown = self._remove_overlapping_text_duplicates(
                            markdown,
                            duplicate_counts,
                        )
                        export_text = getattr(document, "export_to_text", None)
                        plain_text = (
                            await asyncio.to_thread(export_text, page_no=page_number)
                            if export_text
                            else None
                        )
                        page_warnings: list[ParseWarning] = []
                        page_status = PageStatus.COMPLETED
                        if error_messages:
                            page_status = PageStatus.WARNING
                            page_warnings.append(
                                ParseWarning(
                                    code="docling_partial_conversion",
                                    message="Docling reported one or more conversion errors",
                                    page_number=page_number,
                                    backend=self.name,
                                    details={"errors": error_messages[:5]},
                                )
                            )
                        invalid_tables = [
                            fragment
                            for fragment in group_table_fragments
                            if fragment.page_number == page_number and not fragment.valid
                        ]
                        if invalid_tables:
                            page_status = PageStatus.WARNING
                            page_warnings.append(
                                ParseWarning(
                                    code="table_structure_invalid",
                                    message="one or more Docling tables contain invalid or overlapping cell spans",
                                    page_number=page_number,
                                    backend=self.name,
                                    details={
                                        "fragments": [
                                            {
                                                "id": fragment.fragment_id,
                                                "reasons": fragment.invalid_reasons,
                                            }
                                            for fragment in invalid_tables
                                        ]
                                    },
                                )
                            )
                        if duplicate_counts:
                            page_warnings.append(
                                ParseWarning(
                                    code="overlapping_ocr_boxes_deduplicated",
                                    message="identical text from highly overlapping OCR boxes was emitted once",
                                    severity=WarningSeverity.INFO,
                                    page_number=page_number,
                                    backend=self.name,
                                    details={
                                        "removed_items": sum(duplicate_counts.values()),
                                        "minimum_iou": 0.8,
                                    },
                                )
                            )
                        parsed_pages.append(
                            PageParseResult(
                                page_number=page_number,
                                status=page_status,
                                backend=page_backend,
                                content=str(markdown or ""),
                                plain_text=str(plain_text) if plain_text is not None else None,
                                duration_ms=max(
                                    0, round((time.perf_counter() - page_started) * 1000)
                                ),
                                warnings=page_warnings,
                            )
                        )
                    except Exception as exc:
                        parsed_pages.append(
                            PageParseResult(
                                page_number=page_number,
                                status=PageStatus.FAILED,
                                backend=page_backend,
                                duration_ms=max(
                                    0, round((time.perf_counter() - page_started) * 1000)
                                ),
                                warnings=[
                                    ParseWarning(
                                        code="page_export_failed",
                                        message=f"failed to export page: {type(exc).__name__}",
                                        severity=WarningSeverity.ERROR,
                                        page_number=page_number,
                                        backend=self.name,
                                    )
                                ],
                            )
                        )
                    completed_count += 1
                    emitted_status = parsed_pages[-1].status
                    event_name = {
                        PageStatus.COMPLETED: "page.completed",
                        PageStatus.WARNING: "page.warning",
                        PageStatus.FAILED: "page.failed",
                    }[emitted_status]
                    await self._notify(progress_callback, completed_count, len(pages), event_name)
        except (ParserCancelledError, ParserError):
            raise
        except Exception as exc:
            raise ParserError(f"Docling conversion failed: {type(exc).__name__}: {exc}") from exc
        finally:
            if release_slot:
                self._available_slots.put_nowait(worker_slot)

        warnings: list[ParseWarning] = []
        if not glm_status.ready:
            warnings.append(
                ParseWarning(
                    code="glm_ocr_unavailable",
                    message="GLM-OCR was unavailable; Docling ran without remote OCR",
                    backend=self.glm_adapter.name,
                    severity=WarningSeverity.WARNING,
                )
            )
        elapsed = max(0, round((time.perf_counter() - started) * 1000))
        failed = sum(page.status == PageStatus.FAILED for page in parsed_pages)
        route_summary = RouteSummary(failed_pages=failed)
        if force_ocr:
            # FULL_PAGE is one observable GLM crop per selected page; unlike
            # auto routing, these counts do not require inference or guessing.
            route_summary.native_text_pages = 0
            route_summary.pages_with_ocr = len(parsed_pages)
            route_summary.ocr_regions = len(parsed_pages)
            route_summary.vlm_pages = 0
        result = DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=len(parsed_pages) - failed,
            markdown="",
            plain_text="",
            pages=parsed_pages,
            pipeline=ParsePipeline(
                profile=options.profile.value,
                primary=self.name,
                ocr=self.glm_adapter.name if glm_status.ready else None,
            ),
            route_summary=route_summary,
            warnings=warnings,
            usage=ParseUsage(input_bytes=source.size_bytes, duration_ms=elapsed),
        )
        result._picture_candidates = list(picture_candidates)
        result._table_fragments = list(table_fragments)
        return result

    async def close(self) -> None:
        await self.wait_idle()
        self._converters.clear()
        self._initialized = False

    async def wait_idle(self) -> None:
        while self._inflight_conversions:
            await asyncio.gather(*tuple(self._inflight_conversions), return_exceptions=True)
