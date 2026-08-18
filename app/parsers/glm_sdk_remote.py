"""Thin adapter for the official self-hosted GLM-OCR SDK server.

The SDK owns layout detection, region cropping and task-specific recognition.
This adapter deliberately does not reinterpret OCR values: it validates the
region envelope, renders stable Markdown, and exposes table regions through the
same private ``TableFragment`` contract used by the Docling parser.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.models.backend import BackendState, BackendStatus
from app.models.parse_options import ContentParseOptions
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
from app.parsers.base import DocumentParser, ParserCancelledError, ProgressCallback
from app.services.table_service import TableFragment, render_gfm_rows
from app.utils.page_range import parse_page_range
from app.utils.settings import setting


@dataclass(frozen=True, slots=True)
class _HtmlCell:
    text: str
    row_span: int
    col_span: int
    header: bool


class _TableHtmlParser(HTMLParser):
    """Small dependency-free HTML table reader with explicit span handling."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_HtmlCell]] = []
        self._row: list[_HtmlCell] | None = None
        self._cell_tag: str | None = None
        self._cell_parts: list[str] = []
        self._row_span = 1
        self._col_span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            values = {key.casefold(): value for key, value in attrs}
            self._cell_tag = tag
            self._cell_parts = []
            self._row_span = _positive_span(values.get("rowspan"))
            self._col_span = _positive_span(values.get("colspan"))
        elif tag == "br" and self._cell_tag is not None:
            self._cell_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and tag == self._cell_tag and self._row is not None:
            self._row.append(
                _HtmlCell(
                    text=re.sub(r"\s+", " ", "".join(self._cell_parts)).strip(),
                    row_span=self._row_span,
                    col_span=self._col_span,
                    header=tag == "th",
                )
            )
            self._cell_tag = None
            self._cell_parts = []
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _positive_span(value: str | None) -> int:
    try:
        parsed = int(value or "1")
    except ValueError:
        return 1
    return max(1, min(parsed, 2_000))


def _html_table_rows(content: str) -> tuple[list[list[str]], bool]:
    parser = _TableHtmlParser()
    try:
        parser.feed(content)
        parser.close()
    except (ValueError, TypeError):
        return [], False
    if not parser.rows or len(parser.rows) > 2_000:
        return [], False
    anchors: dict[tuple[int, int], str] = {}
    occupied: set[tuple[int, int]] = set()
    columns = 0
    has_header = False
    for row_index, cells in enumerate(parser.rows):
        column_index = 0
        for cell in cells:
            while (row_index, column_index) in occupied:
                column_index += 1
            if column_index + cell.col_span > 100:
                return [], False
            anchors[(row_index, column_index)] = cell.text
            has_header = has_header or (row_index == 0 and cell.header)
            for target_row in range(row_index, row_index + cell.row_span):
                if target_row >= 2_000:
                    return [], False
                for target_column in range(column_index, column_index + cell.col_span):
                    occupied.add((target_row, target_column))
            columns = max(columns, column_index + cell.col_span)
            column_index += cell.col_span
    total_rows = max((position[0] for position in occupied), default=-1) + 1
    if total_rows <= 0 or columns <= 0:
        return [], False
    rows = [
        [anchors.get((row_index, column_index), "") for column_index in range(columns)]
        for row_index in range(total_rows)
    ]
    return rows, has_header


def _markdown_table_rows(content: str) -> list[list[str]]:
    lines = [line.strip() for line in content.splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return []
    rows: list[list[str]] = []
    for index, line in enumerate(lines):
        cells = [
            value.replace(r"\|", "|").strip()
            for value in re.split(r"(?<!\\)\|", line.strip().strip("|"))
        ]
        if index == 1 and cells and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows.append(cells)
    columns = max((len(row) for row in rows), default=0)
    if not rows or columns <= 0 or columns > 100 or len(rows) > 2_000:
        return []
    return [row + [""] * (columns - len(row)) for row in rows]


class GlmSdkRemoteParser(DocumentParser):
    """Parse complete pages through the official ``glmocr`` HTTP service."""

    name = "glm-sdk-remote"

    def __init__(
        self,
        settings: object | None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.enabled = bool(setting(settings, "glm_sdk_enabled", False))
        self.api_url = str(setting(settings, "glm_sdk_url", "http://glm-ocr-sdk:5002/glmocr/parse"))
        self.timeout = float(setting(settings, "glm_sdk_timeout_seconds", 120.0))
        self.scale = float(setting(settings, "glm_sdk_render_scale", 2.0))
        self.max_pixels = int(setting(settings, "glm_sdk_max_image_pixels", 8_000_000))
        self.max_retries = int(setting(settings, "glm_sdk_max_retries", 1))
        self.health_ttl = float(setting(settings, "backend_health_ttl_seconds", 15.0))
        concurrency = int(setting(settings, "glm_sdk_max_concurrency", 1))
        self._semaphore = asyncio.Semaphore(concurrency)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), trust_env=False
        )
        self._last_status: BackendStatus | None = None
        self._last_probe_monotonic = 0.0

    def _health_url(self) -> str:
        parsed = urlsplit(self.api_url)
        path = parsed.path
        if path.endswith("/glmocr/parse"):
            path = path[: -len("/glmocr/parse")] + "/health"
        else:
            path = "/health"
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    async def initialize(self) -> None:
        return None

    async def probe(self, *, force: bool = False) -> BackendStatus:
        if not self.enabled:
            return BackendStatus(
                name=self.name,
                state=BackendState.DISABLED,
                enabled=False,
                detail="official GLM-OCR SDK routing is disabled",
                model="glmocr",
            )
        now = time.monotonic()
        if (
            not force
            and self._last_status is not None
            and now - self._last_probe_monotonic <= self.health_ttl
        ):
            return self._last_status
        started = time.perf_counter()
        try:
            response = await self._client.get(self._health_url())
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ValueError("unexpected health response")
            status = BackendStatus(
                name=self.name,
                state=BackendState.READY,
                detail="official GLM-OCR SDK pipeline responded",
                model="glmocr",
                latency_ms=max(0, round((time.perf_counter() - started) * 1_000)),
            )
        except (httpx.HTTPError, ValueError, TypeError):
            status = BackendStatus(
                name=self.name,
                state=BackendState.UNAVAILABLE,
                detail="health probe failed",
                model="glmocr",
                latency_ms=max(0, round((time.perf_counter() - started) * 1_000)),
            )
        self._last_status = status
        self._last_probe_monotonic = now
        return status

    @staticmethod
    def _cancelled(cancel_event: object | None) -> bool:
        return bool(cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)())

    def _render_page(self, source: StoredSource, page_number: int) -> str:
        if source.mime_type != "application/pdf":
            payload = source.path.read_bytes()
            mime_type = source.mime_type if source.mime_type.startswith("image/") else "image/png"
            return f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"
        import pymupdf

        with pymupdf.open(source.path) as document:
            page = document.load_page(page_number - 1)
            scale = self.scale
            pixels = max(1.0, page.rect.width * scale * page.rect.height * scale)
            if pixels > self.max_pixels:
                scale *= math.sqrt(self.max_pixels / pixels)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            payload = pixmap.tobytes("png")
        return f"data:image/png;base64,{base64.b64encode(payload).decode('ascii')}"

    @staticmethod
    def _page_regions(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("SDK layout JSON is invalid") from exc
        if not isinstance(payload, list):
            raise ValueError("SDK layout is not a list")
        if len(payload) == 1 and isinstance(payload[0], list):
            payload = payload[0]
        if not all(isinstance(item, dict) for item in payload):
            raise ValueError("SDK page contains a non-object region")
        regions: list[dict[str, object]] = []
        for fallback_index, raw in enumerate(payload):
            label = raw.get("label")
            native_label = raw.get("native_label")
            content = raw.get("content")
            bbox = raw.get("bbox_2d")
            if not isinstance(label, str) or not label.strip():
                raise ValueError("SDK region has no label")
            if content is not None and not isinstance(content, str):
                raise ValueError("SDK region content is not text")
            normalized_bbox: list[float] | None = None
            if bbox is not None:
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or not all(isinstance(value, (int, float)) for value in bbox)
                ):
                    raise ValueError("SDK region bbox is invalid")
                normalized_bbox = [float(value) for value in bbox]
                if not all(
                    math.isfinite(value) and 0 <= value <= 1_000 for value in normalized_bbox
                ):
                    raise ValueError("SDK region bbox is out of range")
                if (
                    normalized_bbox[0] > normalized_bbox[2]
                    or normalized_bbox[1] > normalized_bbox[3]
                ):
                    raise ValueError("SDK region bbox is inverted")
            raw_index = raw.get("index", fallback_index)
            index = raw_index if isinstance(raw_index, int) and raw_index >= 0 else fallback_index
            regions.append(
                {
                    "index": index,
                    "label": label.strip().casefold(),
                    "native_label": native_label.casefold()
                    if isinstance(native_label, str)
                    else "",
                    "content": content or "",
                    "bbox_2d": normalized_bbox,
                }
            )

        def region_index(item: dict[str, object]) -> int:
            value = item.get("index")
            return value if isinstance(value, int) else 0

        return sorted(regions, key=region_index)

    @staticmethod
    def _table_fragment(
        region: dict[str, object], page_number: int, ordinal: int
    ) -> TableFragment | None:
        content = str(region.get("content", "") or "").strip()
        rows: list[list[str]]
        has_header = False
        if re.search(r"<table\b", content, re.IGNORECASE):
            rows, has_header = _html_table_rows(content)
        else:
            rows = _markdown_table_rows(content)
        bbox = region.get("bbox_2d")
        if not rows or not isinstance(bbox, list):
            return None
        columns = max(len(row) for row in rows)
        if columns <= 0:
            return None
        left, top, right, bottom = (float(value) / 1_000 for value in bbox)
        fragment_id = f"sdk_p{page_number}_t{ordinal}"
        marker = f"<!-- table-fragment: {fragment_id} -->"
        markdown = render_gfm_rows(rows)
        width = max(0.0, right - left)
        boundaries = [left + width * index / columns for index in range(columns + 1)]
        return TableFragment(
            fragment_id=fragment_id,
            page_number=page_number,
            ordinal=ordinal,
            normalized_bbox=(left, top, right, bottom),
            num_rows=len(rows),
            num_cols=columns,
            rows=rows,
            column_boundaries=[round(value, 6) for value in boundaries],
            markdown=markdown,
            rendered=f"{marker}\n\n{markdown}",
            has_column_header=has_header,
            source_kind="glm",
        )

    @classmethod
    def _render_regions(
        cls, regions: list[dict[str, object]], page_number: int
    ) -> tuple[str, list[TableFragment]]:
        output: list[str] = []
        fragments: list[TableFragment] = []
        table_ordinal = 0
        for region in regions:
            label = str(region["label"])
            native_label = str(region.get("native_label", ""))
            content = str(region.get("content", "") or "").strip()
            if label == "table" or native_label == "table":
                table_ordinal += 1
                fragment = cls._table_fragment(region, page_number, table_ordinal)
                if fragment is not None:
                    fragments.append(fragment)
                    output.append(fragment.rendered)
                elif content:
                    output.append(content)
                continue
            if label == "image" or native_label in {
                "chart",
                "image",
                "footer_image",
                "header_image",
            }:
                output.append("<!-- image -->")
                if content:
                    output.append(content)
                continue
            if not content:
                continue
            if native_label == "doc_title" and not content.lstrip().startswith("#"):
                content = f"# {content}"
            elif native_label == "paragraph_title" and not content.lstrip().startswith("#"):
                content = f"## {content}"
            output.append(content)
        return "\n\n".join(output).strip(), fragments

    async def _request_page(
        self,
        source: StoredSource,
        page_number: int,
        cancel_event: object | None,
    ) -> tuple[PageParseResult, list[TableFragment], int]:
        started = time.perf_counter()
        if self._cancelled(cancel_event):
            raise ParserCancelledError("job was cancelled")
        try:
            image_uri = await asyncio.to_thread(self._render_page, source, page_number)
            payload: dict[str, Any] | None = None
            last_error_code = "glm_sdk_http_error"
            async with self._semaphore:
                for attempt in range(self.max_retries + 1):
                    if self._cancelled(cancel_event):
                        raise ParserCancelledError("job was cancelled")
                    try:
                        response = await self._client.post(
                            self.api_url,
                            json={"images": [image_uri]},
                            headers={"Content-Type": "application/json"},
                        )
                        response.raise_for_status()
                        value = response.json()
                        if not isinstance(value, dict):
                            raise ValueError("SDK response is not an object")
                        if value.get("error"):
                            raise ValueError("SDK response contains an error")
                        payload = value
                        break
                    except httpx.TimeoutException:
                        last_error_code = "glm_sdk_timeout"
                    except httpx.HTTPError:
                        last_error_code = "glm_sdk_http_error"
                    except (ValueError, TypeError):
                        last_error_code = "glm_sdk_invalid_response"
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(2.0, 0.25 * (2**attempt)))
            if payload is None:
                raise RuntimeError(last_error_code)
            layout = payload.get("layout_details", payload.get("json_result"))
            regions = self._page_regions(layout)
            markdown, fragments = self._render_regions(regions, page_number)
            if not markdown:
                markdown_result = payload.get("md_results", payload.get("markdown_result", ""))
                if isinstance(markdown_result, str):
                    markdown = re.sub(
                        r"!\[[^\]]*\]\([^)]*\)", "<!-- image -->", markdown_result
                    ).strip()
            warnings: list[ParseWarning] = []
            status = PageStatus.COMPLETED
            if not markdown:
                status = PageStatus.FAILED
                warnings.append(
                    ParseWarning(
                        code="glm_sdk_empty_page",
                        message="the official GLM-OCR SDK returned no usable page content",
                        severity=WarningSeverity.ERROR,
                        page_number=page_number,
                        backend=self.name,
                    )
                )
            return (
                PageParseResult(
                    page_number=page_number,
                    status=status,
                    backend=self.name,
                    content=markdown or None,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1_000)),
                    warnings=warnings,
                ),
                fragments,
                len(regions),
            )
        except ParserCancelledError:
            raise
        except Exception as exc:
            code = str(exc) if str(exc).startswith("glm_sdk_") else "glm_sdk_invalid_response"
            return (
                PageParseResult(
                    page_number=page_number,
                    status=PageStatus.FAILED,
                    backend=self.name,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1_000)),
                    warnings=[
                        ParseWarning(
                            code=code,
                            message="the official GLM-OCR SDK page candidate could not be produced",
                            severity=WarningSeverity.ERROR,
                            page_number=page_number,
                            backend=self.name,
                        )
                    ],
                ),
                [],
                0,
            )

    async def parse(
        self,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        document_id: str,
        progress_callback: ProgressCallback | None = None,
        cancel_event: object | None = None,
    ) -> DocumentParseResult:
        if not self.enabled:
            from app.parsers.base import ParserUnavailableError

            raise ParserUnavailableError("official GLM-OCR SDK routing is disabled")
        pages = parse_page_range(options.page_range, source.page_count)
        started = time.perf_counter()

        async def notify(completed: int, event: str) -> None:
            if progress_callback is None:
                return
            value = progress_callback(completed, len(pages), event)
            if asyncio.iscoroutine(value):
                await value

        await notify(0, "document.started")
        completed = 0

        async def run(page_number: int) -> tuple[PageParseResult, list[TableFragment], int]:
            nonlocal completed
            page_started = time.perf_counter()
            try:
                async with asyncio.timeout(self.timeout):
                    value = await self._request_page(source, page_number, cancel_event)
            except TimeoutError:
                value = (
                    PageParseResult(
                        page_number=page_number,
                        status=PageStatus.FAILED,
                        backend=self.name,
                        duration_ms=min(
                            max(0, round((time.perf_counter() - page_started) * 1_000)),
                            max(0, round(self.timeout * 1_000)),
                        ),
                        warnings=[
                            ParseWarning(
                                code="glm_sdk_timeout",
                                message=(
                                    "the official GLM-OCR SDK page candidate exceeded "
                                    "the complete per-page time budget"
                                ),
                                severity=WarningSeverity.ERROR,
                                page_number=page_number,
                                backend=self.name,
                                details={"budget_seconds": self.timeout},
                            )
                        ],
                    ),
                    [],
                    0,
                )
            completed += 1
            event = "page.failed" if value[0].status == PageStatus.FAILED else "page.completed"
            await notify(completed, event)
            return value

        # Keep page requests ordered. The official SDK already parallelizes OCR
        # regions inside a page, while starting every page task at once made the
        # per-page diagnostic duration include semaphore queue time. More
        # importantly, a timed-out page must stop later candidate calls so the
        # primary parser result remains available within a bounded budget.
        page_outputs: list[tuple[PageParseResult, list[TableFragment], int]] = []
        stop_after_timeout = False
        for page_number in pages:
            if stop_after_timeout:
                completed += 1
                page_outputs.append(
                    (
                        PageParseResult(
                            page_number=page_number,
                            status=PageStatus.FAILED,
                            backend=self.name,
                            duration_ms=0,
                            warnings=[
                                ParseWarning(
                                    code="glm_sdk_skipped_after_timeout",
                                    message=(
                                        "the official GLM-OCR SDK page candidate was skipped "
                                        "after an earlier page exhausted the time budget"
                                    ),
                                    severity=WarningSeverity.ERROR,
                                    page_number=page_number,
                                    backend=self.name,
                                )
                            ],
                        ),
                        [],
                        0,
                    )
                )
                await notify(completed, "page.failed")
                continue
            value = await run(page_number)
            page_outputs.append(value)
            stop_after_timeout = any(
                warning.code == "glm_sdk_timeout" for warning in value[0].warnings
            )
        parsed_pages = sorted((item[0] for item in page_outputs), key=lambda page: page.page_number)
        fragments = [fragment for item in page_outputs for fragment in item[1]]
        region_count = sum(item[2] for item in page_outputs)
        failed = sum(page.status == PageStatus.FAILED for page in parsed_pages)
        markdown = "\n\n\f\n\n".join(page.content or "" for page in parsed_pages)
        result = DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=len(parsed_pages) - failed,
            markdown=markdown,
            plain_text="",
            pages=parsed_pages,
            pipeline=ParsePipeline(
                profile=options.profile.value,
                primary=self.name,
                ocr=self.name,
            ),
            route_summary=RouteSummary(
                native_text_pages=0,
                pages_with_ocr=len(parsed_pages) - failed,
                ocr_regions=region_count,
                vlm_pages=0,
                failed_pages=failed,
            ),
            usage=ParseUsage(
                input_bytes=source.size_bytes,
                duration_ms=max(0, round((time.perf_counter() - started) * 1_000)),
            ),
        )
        result._table_fragments.extend(fragments)
        return result

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
