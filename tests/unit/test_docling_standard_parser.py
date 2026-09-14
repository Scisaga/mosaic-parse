from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

import pymupdf
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
    ScanPolicy,
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


def test_page_groups_are_split_into_balanced_contiguous_chunks() -> None:
    assert DoclingStandardParser._split_page_group((1, 6), 3) == [(1, 2), (3, 4), (5, 6)]
    assert DoclingStandardParser._split_page_group((4, 8), 3) == [(4, 5), (6, 7), (8, 8)]


def test_parallelism_native_text_preflight_rejects_scanned_pages(tmp_path: Path) -> None:
    native_path = tmp_path / "native.pdf"
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page()
        page.insert_text((72, 72), "native text " * 10)
    document.save(native_path)
    document.close()
    parser = DoclingStandardParser(
        SimpleNamespace(parser_workers=1, quality_sparse_native_characters=40),
        StubGlmAdapter(),  # type: ignore[arg-type]
    )
    native_source = StoredSource(
        path=native_path,
        filename=native_path.name,
        mime_type="application/pdf",
        size_bytes=native_path.stat().st_size,
        page_count=2,
    )

    assert parser._is_native_pdf_for_parallelism(native_source, (1, 2)) is True

    scanned_path = tmp_path / "scanned.pdf"
    document = pymupdf.open()
    document.new_page()
    document.save(scanned_path)
    document.close()
    scanned_source = StoredSource(
        path=scanned_path,
        filename=scanned_path.name,
        mime_type="application/pdf",
        size_bytes=scanned_path.stat().st_size,
        page_count=1,
    )

    assert parser._is_native_pdf_for_parallelism(scanned_source, (1, 1)) is False


async def test_native_pdf_uses_cached_converters_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubGlmAdapter()
    parser = DoclingStandardParser(
        SimpleNamespace(
            parser_workers=1,
            docling_num_threads=4,
            docling_page_workers=3,
            docling_page_parallel_min_pages=4,
        ),
        adapter,  # type: ignore[arg-type]
    )
    parser._initialized = True
    monkeypatch.setattr(parser, "_is_native_pdf_for_parallelism", lambda *_args: True)
    converters = [RecordingConverter() for _ in range(3)]
    for slot, converter in enumerate(converters):
        key = (slot, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
        parser._converters[key] = converter

    result = await parser.parse(
        source(6),
        ContentParseOptions(),
        document_id="docparse_parallel_native",
    )

    assert [converter.page_ranges for converter in converters] == [
        [(1, 2)],
        [(3, 4)],
        [(5, 6)],
    ]
    assert [page.page_number for page in result.pages] == [1, 2, 3, 4, 5, 6]
    assert parser._available_slots.qsize() == 3


async def test_non_native_pdf_stays_on_one_cached_converter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubGlmAdapter()
    parser = DoclingStandardParser(
        SimpleNamespace(
            parser_workers=1,
            docling_page_workers=3,
            docling_page_parallel_min_pages=4,
        ),
        adapter,  # type: ignore[arg-type]
    )
    parser._initialized = True
    monkeypatch.setattr(parser, "_is_native_pdf_for_parallelism", lambda *_args: False)
    converters = [RecordingConverter() for _ in range(3)]
    for slot, converter in enumerate(converters):
        key = (slot, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
        parser._converters[key] = converter

    result = await parser.parse(
        source(6),
        ContentParseOptions(),
        document_id="docparse_single_scanned",
    )

    assert [converter.page_ranges for converter in converters] == [[(1, 6)], [], []]
    assert [page.page_number for page in result.pages] == [1, 2, 3, 4, 5, 6]
    assert parser._available_slots.qsize() == 3


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


async def test_cancelled_parallel_conversion_keeps_all_slots_until_threads_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = StubGlmAdapter()
    parser = DoclingStandardParser(
        SimpleNamespace(
            parser_workers=1,
            docling_page_workers=3,
            docling_page_parallel_min_pages=4,
        ),
        adapter,  # type: ignore[arg-type]
    )
    parser._initialized = True
    monkeypatch.setattr(parser, "_is_native_pdf_for_parallelism", lambda *_args: True)
    converters = [BlockingConverter() for _ in range(3)]
    for slot, converter in enumerate(converters):
        key = (slot, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
        parser._converters[key] = converter

    parsing = asyncio.create_task(
        parser.parse(
            source(6),
            ContentParseOptions(),
            document_id="docparse_cancelled_parallel_slots",
        )
    )
    for converter in converters:
        assert await asyncio.to_thread(converter.entered.wait, 2)
    parsing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parsing
    assert parser._available_slots.qsize() == 0

    for converter in converters:
        converter.release.set()
    await parser.wait_idle()
    await asyncio.sleep(0)
    assert parser._available_slots.qsize() == 3


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


@pytest.mark.parametrize("glm_state", [BackendState.READY, BackendState.UNAVAILABLE])
async def test_skip_scan_policy_disables_pdf_ocr_without_backend_warning(
    glm_state: BackendState,
) -> None:
    adapter = StubGlmAdapter(glm_state)
    parser = DoclingStandardParser(SimpleNamespace(parser_workers=1), adapter)  # type: ignore[arg-type]
    parser._initialized = True
    converter = RecordingConverter()
    key = (0, ParseProfile.BALANCED.value, False, ("zh", "en"), False)
    parser._converters[key] = converter

    result = await parser.parse(
        source(1),
        ContentParseOptions(scan_policy=ScanPolicy.SKIP),
        document_id="docparse_skip_ocr",
    )

    assert converter.page_ranges == [(1, 1)]
    assert result.pipeline.ocr is None
    assert result.warnings == []
