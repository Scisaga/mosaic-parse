from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from docling_core.types.doc import (
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ProvenanceItem,
    Size,
)

from app.models import (
    BackendState,
    BackendStatus,
    ContentParseOptions,
    ParseProfile,
    StoredSource,
)
from app.parsers.docling_standard import DoclingStandardParser


class StubGlmAdapter:
    name = "glm-ocr-remote"

    def __init__(self, state: BackendState = BackendState.UNAVAILABLE) -> None:
        self.state = state

    async def probe(self, *, force: bool = False) -> BackendStatus:
        return BackendStatus(
            name=self.name, state=self.state, enabled=self.state != BackendState.DISABLED
        )


class FakeDocument:
    def __init__(self, page_group: tuple[int, int]) -> None:
        self.page_group = page_group

    def export_to_markdown(self, *, page_no: int) -> str:
        assert self.page_group[0] <= page_no <= self.page_group[1]
        return f"# Page {page_no}"

    def export_to_text(self, *, page_no: int) -> str:
        assert self.page_group[0] <= page_no <= self.page_group[1]
        return f"Page {page_no}"


class FakeConversion:
    status = "success"
    errors: list[object] = []

    def __init__(self, page_group: tuple[int, int]) -> None:
        self.document = FakeDocument(page_group)


class RecordingConverter:
    def __init__(self) -> None:
        self.page_ranges: list[tuple[int, int]] = []

    def convert(self, **kwargs: object) -> FakeConversion:
        page_group = kwargs["page_range"]
        assert isinstance(page_group, tuple)
        self.page_ranges.append(page_group)
        return FakeConversion(page_group)


class BlockingConverter(RecordingConverter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def convert(self, **kwargs: object) -> FakeConversion:
        self.entered.set()
        assert self.release.wait(timeout=5)
        return super().convert(**kwargs)


class PipelineProgressParser(DoclingStandardParser):
    """Test the thread-to-async progress bridge without importing Docling internals."""

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
        assert progress_sink is not None
        for page_number in range(page_group[0], page_group[1] + 1):
            progress_sink.put(page_number)
        return super()._convert_in_slot(
            worker_slot,
            source,
            page_group,
            options,
            force_ocr,
            glm_ready,
        )


def source(page_count: int = 1000) -> StoredSource:
    return StoredSource(
        path=Path("/tmp/sparse.pdf"),
        filename="sparse.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        page_count=page_count,
    )


def test_empty_download_directory_clears_upstream_offline_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from docling.datamodel.settings import settings as docling_settings

    monkeypatch.setattr(docling_settings, "artifacts_path", tmp_path)
    parser = DoclingStandardParser(
        SimpleNamespace(
            parser_workers=1,
            docling_artifacts_path=tmp_path,
            docling_model_download=True,
        ),
        StubGlmAdapter(),  # type: ignore[arg-type]
    )

    parser._build_converter(ParseProfile.BALANCED, False, ["zh", "en"], False)

    assert docling_settings.artifacts_path is None


def test_picture_candidates_keep_only_caption_and_normalized_crop_geometry() -> None:
    document = DoclingDocument(name="diagram")
    document.add_page(page_no=1, size=Size(width=100, height=200))
    caption = document.add_text(label=DocItemLabel.CAPTION, text="图 A.1 装配流程图")
    document.add_picture(
        caption=caption,
        prov=ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(
                l=20,
                t=160,
                r=80,
                b=40,
                coord_origin=CoordOrigin.BOTTOMLEFT,
            ),
            charspan=(0, 0),
        ),
    )

    candidates = DoclingStandardParser._extract_picture_candidates(document, (1, 1))

    assert len(candidates) == 1
    assert candidates[0].page_number == 1
    assert candidates[0].placeholder_index == 0
    assert candidates[0].caption == "图 A.1 装配流程图"
    assert candidates[0].normalized_bbox == pytest.approx((0.2, 0.2, 0.8, 0.8))


async def test_sparse_page_range_is_converted_as_independent_groups() -> None:
    adapter = StubGlmAdapter()
    parser = DoclingStandardParser(SimpleNamespace(parser_workers=1), adapter)  # type: ignore[arg-type]
    parser._initialized = True
    converter = RecordingConverter()
    key = (0, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
    parser._converters[key] = converter
    progress: list[tuple[int, int, str]] = []

    async def report(current: int, total: int, state: str) -> None:
        progress.append((current, total, state))

    result = await parser.parse(
        source(),
        ContentParseOptions(unit_range="1,1000"),
        document_id="docparse_sparse",
        progress_callback=report,
    )

    assert converter.page_ranges == [(1, 1), (1000, 1000)]
    assert [page.page_number for page in result.pages] == [1, 1000]
    assert progress == [
        (0, 2, "document.started"),
        (1, 2, "page.completed"),
        (2, 2, "page.completed"),
    ]


async def test_contiguous_conversion_streams_real_pipeline_page_progress() -> None:
    adapter = StubGlmAdapter()
    parser = PipelineProgressParser(SimpleNamespace(parser_workers=1), adapter)  # type: ignore[arg-type]
    parser._initialized = True
    converter = RecordingConverter()
    key = (0, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
    parser._converters[key] = converter
    progress: list[tuple[int, int, str]] = []

    async def report(current: int, total: int, state: str) -> None:
        progress.append((current, total, state))

    result = await parser.parse(
        source(3),
        ContentParseOptions(),
        document_id="docparse_streaming_progress",
        progress_callback=report,
    )

    assert converter.page_ranges == [(1, 3)]
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert progress == [
        (0, 3, "document.started"),
        (1, 3, "page.processed"),
        (2, 3, "page.processed"),
        (3, 3, "page.processed"),
        (1, 3, "page.completed"),
        (2, 3, "page.completed"),
        (3, 3, "page.completed"),
    ]


async def test_cancelled_conversion_keeps_worker_slot_until_thread_exits() -> None:
    adapter = StubGlmAdapter()
    parser = DoclingStandardParser(SimpleNamespace(parser_workers=1), adapter)  # type: ignore[arg-type]
    parser._initialized = True
    converter = BlockingConverter()
    key = (0, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
    parser._converters[key] = converter

    parsing = asyncio.create_task(
        parser.parse(
            source(1),
            ContentParseOptions(),
            document_id="docparse_cancelled_slot",
        )
    )
    assert await asyncio.to_thread(converter.entered.wait, 2)
    parsing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parsing
    assert parser._available_slots.qsize() == 0

    converter.release.set()
    await parser.wait_idle()
    await asyncio.sleep(0)
    assert parser._available_slots.qsize() == 1


async def test_automatic_route_reports_unavailable_optional_glm() -> None:
    adapter = StubGlmAdapter(BackendState.UNAVAILABLE)
    parser = DoclingStandardParser(SimpleNamespace(parser_workers=1), adapter)  # type: ignore[arg-type]
    parser._initialized = True
    converter = RecordingConverter()
    key = (0, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
    parser._converters[key] = converter

    result = await parser.parse(source(1), ContentParseOptions(), document_id="docparse_degraded")

    assert [warning.code for warning in result.warnings] == ["glm_ocr_unavailable"]
    assert converter.page_ranges == [(1, 1)]
