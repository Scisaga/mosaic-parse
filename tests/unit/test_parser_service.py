from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import (
    ContentParseOptions,
    DocumentParseResult,
    PageParseResult,
    ParsePipeline,
    RouteSummary,
    ServiceError,
    StoredSource,
)
from app.parsers import ParserUnavailableError
from app.services.parser_service import (
    ParserService,
    _NativePageEvidence,
)


def stored_source() -> StoredSource:
    path = Path("tests/fixtures/native-report.pdf").resolve()
    return StoredSource(
        path=path,
        filename=path.name,
        mime_type="application/pdf",
        size_bytes=path.stat().st_size,
        page_count=1,
    )


def result(document_id: str, content: str = "# Page 1") -> DocumentParseResult:
    return DocumentParseResult(
        document_id=document_id,
        filename="native-report.pdf",
        mime_type="application/pdf",
        page_count=1,
        processed_pages=1,
        pages=[
            PageParseResult(
                page_number=1,
                backend="fake",
                content=content,
                plain_text=content,
            )
        ],
        pipeline=ParsePipeline(profile="balanced", primary="fake"),
        route_summary=RouteSummary(failed_pages=0),
    )


class ImmediateParser:
    name = "fake"

    def __init__(self, content: str = "# Page 1") -> None:
        self.calls = 0
        self.content = content

    async def initialize(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def parse(
        self, source, options, *, document_id, progress_callback=None, cancel_event=None
    ):
        self.calls += 1
        return result(document_id, self.content)


class BlockingParser(ImmediateParser):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def parse(
        self, source, options, *, document_id, progress_callback=None, cancel_event=None
    ):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.finished.set()
        return result(document_id)


class UnavailableParser(ImmediateParser):
    async def parse(
        self, source, options, *, document_id, progress_callback=None, cancel_event=None
    ):
        self.calls += 1
        raise ParserUnavailableError(
            "primary parser is unavailable",
            details={"backend": "fake", "state": "unavailable"},
        )


def install_parser(service: ParserService, parser: ImmediateParser, monkeypatch) -> None:
    service.standard_parser = parser  # type: ignore[assignment]
    service._initialized = True

    async def no_page_evidence(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(service, "_page_evidence", no_page_evidence)


async def test_total_timeout_includes_waiting_for_parser_semaphore(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, content_timeout_seconds=0.05))
    parser = ImmediateParser()
    install_parser(service, parser, monkeypatch)
    await service._semaphore.acquire()
    try:
        with pytest.raises(ServiceError) as caught:
            await service.parse(
                stored_source(), ContentParseOptions(), document_id="docparse_timeout"
            )
        assert caught.value.code == "parse_timeout"
        assert caught.value.status_code == 504
        assert parser.calls == 0
        assert service._active_parses == 0
    finally:
        service._semaphore.release()


async def test_backend_unavailable_preserves_probe_details(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, content_timeout_seconds=1.0))
    parser = UnavailableParser()
    install_parser(service, parser, monkeypatch)

    with pytest.raises(ServiceError) as caught:
        await service.parse(
            stored_source(), ContentParseOptions(), document_id="docparse_unavailable"
        )
    assert caught.value.code == "backend_unavailable"
    assert caught.value.details == {
        "profile": "balanced",
        "backend": "fake",
        "state": "unavailable",
    }


async def test_reload_waits_for_active_parse_and_uses_rebuilt_parser(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=2, content_timeout_seconds=2.0))
    old_parser = BlockingParser()
    new_parser = ImmediateParser()
    install_parser(service, old_parser, monkeypatch)
    close_started = asyncio.Event()

    async def fake_close_adapters() -> None:
        assert old_parser.finished.is_set()
        close_started.set()
        service._initialized = False

    def fake_build_adapters() -> None:
        service.standard_parser = new_parser  # type: ignore[assignment]

    async def fake_initialize() -> None:
        service._initialized = True

    monkeypatch.setattr(service, "_close_adapters", fake_close_adapters)
    monkeypatch.setattr(service, "_build_adapters", fake_build_adapters)
    monkeypatch.setattr(service, "initialize", fake_initialize)

    first = asyncio.create_task(
        service.parse(stored_source(), ContentParseOptions(), document_id="docparse_old")
    )
    await old_parser.entered.wait()
    reloading = asyncio.create_task(service.reload())
    second = asyncio.create_task(
        service.parse(stored_source(), ContentParseOptions(), document_id="docparse_new")
    )
    await asyncio.sleep(0)
    assert new_parser.calls == 0

    old_parser.release.set()
    await first
    await reloading
    await second
    assert close_started.is_set()
    assert old_parser.calls == 1
    assert new_parser.calls == 1


async def test_parse_always_materializes_document_evidence_ir(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, content_timeout_seconds=1.0))
    install_parser(service, ImmediateParser("Evidence text"), monkeypatch)

    parsed = await service.parse(stored_source(), ContentParseOptions(), document_id="docparse_ir")

    assert parsed.evidence_ir is not None
    assert parsed.evidence_ir.object == "content.evidence"
    assert parsed.evidence_ir.source.content_id == "docparse_ir"
    assert parsed.evidence_ir.units[0].blocks[0].text == "Evidence text"
    assert parsed.evidence_ir.renderings.markdown.endswith("Evidence text")


async def test_include_renderings_false_does_not_remove_evidence(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, content_timeout_seconds=1.0))
    install_parser(service, ImmediateParser("Evidence text"), monkeypatch)

    parsed = await service.parse(
        stored_source(),
        ContentParseOptions(include_renderings=False),
        document_id="docparse_ir_no_rendering",
    )

    assert parsed.evidence_ir is not None
    assert parsed.evidence_ir.units[0].blocks[0].text == "Evidence text"
    assert parsed.evidence_ir.renderings.markdown == ""
    assert parsed.evidence_ir.units[0].renderings.markdown == ""


async def test_balanced_skips_visual_and_accurate_invokes_visual_once(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, content_timeout_seconds=1.0))
    install_parser(service, ImmediateParser("content"), monkeypatch)
    calls = 0

    async def visual(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(service, "_apply_visual_fusion", visual)
    await service.parse(
        stored_source(),
        ContentParseOptions(profile="balanced"),
        document_id="docparse_balanced",
    )
    await service.parse(
        stored_source(),
        ContentParseOptions(profile="accurate"),
        document_id="docparse_accurate",
    )
    assert calls == 1


def test_text_normalization_uses_native_tokens_and_preserves_code_and_mermaid() -> None:
    original = (
        "制造企业 已经拥 有大量SOP，M ES、An d roi d 与 Physical World Model。\n"
        "G B /T 1243 -2024；ISO 9 001 : 201 5。\n"
        "| M ES | 制 造 |\n"
        "`M E S 中 文`\n"
        "```python\nM E S = '中 文'\n```\n"
        "```mermaid\nflowchart TD\nn1[中 文] --> n2[M E S]\n```\n"
        "- \uf0b7 现场 作业\ufffd\u200b\n"
    )

    normalized, glyphs = ParserService._normalize_markdown_text(
        original,
        {
            "mes",
            "android",
            "physical",
            "world",
            "model",
            "gb",
            "t",
            "1243",
            "2024",
            "iso",
            "9001",
            "2015",
        },
        {"gb/t", "1243-2024", "9001:2015"},
    )

    assert "制造企业已经拥有大量SOP，MES、Android 与 Physical World Model。" in normalized
    assert "GB/T 1243-2024；ISO 9001:2015。" in normalized
    assert "| MES | 制造 |" in normalized
    assert "`M E S 中 文`" in normalized
    assert "```python\nM E S = '中 文'\n```" in normalized
    assert "```mermaid\nflowchart TD\nn1[中 文] --> n2[M E S]\n```" in normalized
    assert glyphs == {"U+F0B7": 1, "U+FFFD": 1, "U+200B": 1}


def test_glyph_sanitizer_never_guesses_pua_without_font_position_evidence() -> None:
    original = "- \uf0b7 item\ufffd\u200c\u200d\ue123\f"
    normalized, glyphs = ParserService._sanitize_glyphs(original)
    assert normalized == original
    assert glyphs == {
        "U+000C": 1,
        "U+200C": 1,
        "U+200D": 1,
        "U+E123": 1,
        "U+F0B7": 1,
        "U+FFFD": 1,
    }


async def test_automatic_text_normalization_uses_native_lexicon(monkeypatch) -> None:
    service = ParserService(
        SimpleNamespace(parser_workers=1, content_timeout_seconds=1.0, vlm_enabled=False)
    )
    install_parser(
        service,
        ImmediateParser("制造企业 已经拥 有，M ES 与 Physical World Model。"),
        monkeypatch,
    )
    monkeypatch.setattr(
        service,
        "_native_lexicons",
        lambda _source, _pages: {
            1: _NativePageEvidence(words={"mes", "physical", "world", "model"})
        },
    )

    parsed = await service.parse(
        stored_source(), ContentParseOptions(), document_id="docparse_spacing"
    )
    expected = "制造企业已经拥有，MES 与 Physical World Model。"
    assert parsed.pages[0].content == expected
    assert parsed.evidence_ir is not None
    assert parsed.evidence_ir.units[0].blocks[0].text == expected
