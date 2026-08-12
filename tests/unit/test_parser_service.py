from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import (
    BackendState,
    BackendStatus,
    DocumentParseOptions,
    DocumentParseResult,
    PageParseResult,
    ParseMode,
    ParsePipeline,
    ParseWarning,
    RouteSummary,
    ServiceError,
    StoredSource,
)
from app.parsers import ParserUnavailableError
from app.parsers.docling_standard import PictureCandidate
from app.services.parser_service import (
    ParserService,
    _NativeIdentifierEvidence,
    _NativePageEvidence,
)


def stored_source() -> StoredSource:
    return StoredSource(
        path=Path("/tmp/parser-service.pdf"),
        filename="parser-service.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        page_count=1,
    )


def result(document_id: str) -> DocumentParseResult:
    return DocumentParseResult(
        document_id=document_id,
        filename="parser-service.pdf",
        mime_type="application/pdf",
        page_count=1,
        processed_pages=1,
        pages=[PageParseResult(page_number=1, backend="fake", content="# Page 1")],
        pipeline=ParsePipeline(mode="auto", profile="balanced", primary="fake"),
        route_summary=RouteSummary(failed_pages=0),
    )


class ImmediateParser:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        self.calls += 1
        return result(document_id)


class BlockingParser(ImmediateParser):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.finished.set()
        return result(document_id)


class UnavailableParser(ImmediateParser):
    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        self.calls += 1
        raise ParserUnavailableError(
            "OCR mode requires a ready GLM-OCR backend",
            details={"backend": "glm-ocr-remote", "state": "unavailable"},
        )


class StaticParser(ImmediateParser):
    def __init__(self, content: str, *, backend: str = "fake") -> None:
        super().__init__()
        self.content = content
        self.backend = backend
        self.picture_candidates: list[PictureCandidate] = []

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        self.calls += 1
        parsed = result(document_id)
        parsed.pages[0].content = self.content
        parsed.pages[0].plain_text = self.content
        parsed.pages[0].backend = self.backend
        parsed._picture_candidates = list(self.picture_candidates)
        return parsed


class StaticProbe:
    name = "glm-ocr-remote"

    def __init__(self, state: BackendState) -> None:
        self.state = state

    async def probe(self, *, force: bool = False) -> BackendStatus:
        return BackendStatus(name=self.name, state=self.state)


class DiagramVlm(StaticProbe):
    name = "ollama-vlm"

    def __init__(self, mermaid: str) -> None:
        super().__init__(BackendState.READY)
        self.mermaid = mermaid
        self.calls: list[dict[str, object]] = []

    async def diagram_to_mermaid(self, source, **kwargs):
        self.calls.append(kwargs)
        return self.mermaid


class RepairVlm(StaticParser):
    name = "ollama-vlm"

    async def probe(self, *, force: bool = False) -> BackendStatus:
        return BackendStatus(name=self.name, state=BackendState.READY)


class FailingDiagramVlm(DiagramVlm):
    async def diagram_to_mermaid(self, source, **kwargs):
        self.calls.append(kwargs)
        from app.parsers import ParserError

        raise ParserError("strict validation failed")


class UnexpectedDiagramVlm(DiagramVlm):
    async def diagram_to_mermaid(self, source, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("unexpected client decoding bug")


class CrashingDiagramVlm(DiagramVlm):
    async def diagram_to_mermaid(self, source, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("unexpected crop renderer failure")


async def test_total_timeout_includes_waiting_for_parser_semaphore() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=0.05))
    parser = ImmediateParser()
    service.registry.register(ParseMode.AUTO, parser)  # type: ignore[arg-type]
    service._initialized = True
    await service._semaphore.acquire()
    try:
        with pytest.raises(ServiceError) as caught:
            await service.parse(stored_source(), DocumentParseOptions(), document_id="docparse_timeout")
        assert caught.value.code == "parse_timeout"
        assert caught.value.status_code == 504
        assert parser.calls == 0
        assert service._active_parses == 0
    finally:
        service._semaphore.release()
        await service.close()


async def test_backend_unavailable_preserves_backend_probe_details() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    parser = UnavailableParser()
    service.registry.register(ParseMode.OCR, parser)  # type: ignore[arg-type]
    service._initialized = True
    try:
        with pytest.raises(ServiceError) as caught:
            await service.parse(
                stored_source(),
                DocumentParseOptions(mode=ParseMode.OCR),
                document_id="docparse_unavailable",
            )
        assert caught.value.code == "backend_unavailable"
        assert caught.value.status_code == 502
        assert caught.value.details == {
            "mode": "ocr",
            "backend": "glm-ocr-remote",
            "state": "unavailable",
        }
    finally:
        await service.close()


async def test_reload_waits_for_active_parse_and_new_parse_uses_new_adapter(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=2, document_timeout_seconds=2.0))
    old_parser = BlockingParser()
    new_parser = ImmediateParser()
    service.registry.register(ParseMode.AUTO, old_parser)  # type: ignore[arg-type]
    service._initialized = True
    close_started = asyncio.Event()

    async def fake_close_adapters() -> None:
        assert old_parser.finished.is_set()
        close_started.set()
        service._initialized = False

    def fake_build_adapters() -> None:
        service.registry.register(ParseMode.AUTO, new_parser)  # type: ignore[arg-type]

    async def fake_initialize() -> None:
        service._initialized = True

    monkeypatch.setattr(service, "_close_adapters", fake_close_adapters)
    monkeypatch.setattr(service, "_build_adapters", fake_build_adapters)
    monkeypatch.setattr(service, "initialize", fake_initialize)

    first = asyncio.create_task(service.parse(stored_source(), DocumentParseOptions(), document_id="docparse_old"))
    await old_parser.entered.wait()
    reloading = asyncio.create_task(service.reload())
    await asyncio.sleep(0)
    assert not close_started.is_set()
    second = asyncio.create_task(service.parse(stored_source(), DocumentParseOptions(), document_id="docparse_new"))
    await asyncio.sleep(0)
    assert new_parser.calls == 0

    old_parser.release.set()
    await first
    await reloading
    await second

    assert close_started.is_set()
    assert old_parser.calls == 1
    assert new_parser.calls == 1


async def test_auto_repairs_suspicious_unicode_with_full_page_ocr_and_reports_phase() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser("犚 犲 犿 犪 狀 狌 犳 犪 犮 狋 狌 狉 犻 狀 犵")
    ocr = StaticParser("Remanufacturing", backend="glm-ocr-remote")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.standard_parser = ocr  # type: ignore[assignment]
    service.glm_adapter = StaticProbe(BackendState.READY)  # type: ignore[assignment]
    service._initialized = True
    progress: list[tuple[int, int, str]] = []

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_auto_repair",
        progress_callback=lambda current, total, phase: progress.append((current, total, phase)),
    )

    assert parsed.pages[0].content == "Remanufacturing"
    assert parsed.pages[0].backend == "glm-ocr-remote"
    assert parsed.route_summary.pages_with_ocr == 1
    assert parsed.route_summary.ocr_regions == 1
    assert [warning.code for warning in parsed.warnings] == ["auto_mojibake_repaired"]
    assert progress == [(1, 1, "postprocess.text_repair")]


async def test_standard_mode_never_applies_auto_text_repair() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser("犚 犲 犿 犪 狀 狌 犳 犪 犮 狋 狌 狉 犻 狀 犵")
    repair = StaticParser("Remanufacturing", backend="glm-ocr-remote")
    service.registry.register(ParseMode.STANDARD, primary)  # type: ignore[arg-type]
    service.standard_parser = repair  # type: ignore[assignment]
    service.glm_adapter = StaticProbe(BackendState.READY)  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.STANDARD),
        document_id="docparse_standard_no_repair",
    )

    assert "犚" in (parsed.pages[0].content or "")
    assert repair.calls == 0


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
        {"mes", "android", "physical", "world", "model", "gb", "t", "1243", "2024", "iso", "9001", "2015"},
        {"gb/t", "1243-2024", "9001:2015"},
    )

    assert "制造企业已经拥有大量SOP，MES、Android 与 Physical World Model。" in normalized
    assert "GB/T 1243-2024；ISO 9001:2015。" in normalized
    assert "| MES | 制造 |" in normalized
    assert "`M E S 中 文`" in normalized
    assert "```python\nM E S = '中 文'\n```" in normalized
    assert "```mermaid\nflowchart TD\nn1[中 文] --> n2[M E S]\n```" in normalized
    assert "- 现场作业" in normalized
    assert glyphs == {"U+F0B7": 1, "U+FFFD": 1, "U+200B": 1}


def test_glyph_sanitizer_maps_only_f0b7_and_preserves_suspicious_glyphs() -> None:
    original = "- \uf0b7 item\ufffd\u200c\u200d\ue123\f"

    normalized, glyphs = ParserService._sanitize_glyphs(original)

    assert normalized == "- item\ufffd\u200c\u200d\ue123\f"
    assert glyphs == {
        "U+000C": 1,
        "U+200C": 1,
        "U+200D": 1,
        "U+E123": 1,
        "U+F0B7": 1,
        "U+FFFD": 1,
    }


def test_code_regions_preserve_all_glyphs_without_mapping_f0b7() -> None:
    inline = "`inline \uf0b7\ufffd\u200c\u200d\ue123`"
    fenced = "```text\nfenced \uf0b7\ufffd\u200c\u200d\ue123\f\n```"
    original = f"{inline}\n{fenced}\n"

    normalized, glyphs = ParserService._normalize_markdown_text(original, set(), set())

    assert normalized == original
    assert "U+F0B7" not in glyphs
    assert glyphs == {
        "U+000C": 1,
        "U+200C": 2,
        "U+200D": 2,
        "U+E123": 2,
        "U+FFFD": 2,
    }


async def test_auto_normalizes_content_and_plain_text_but_standard_does_not(monkeypatch) -> None:
    source_text = "制造企业 已经拥 有，M ES 与 Physical World Model。"
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False))
    primary = StaticParser(source_text)
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service._initialized = True
    monkeypatch.setattr(
        service,
        "_native_lexicons",
        lambda _source, _pages: {
            1: _NativePageEvidence(words={"mes", "physical", "world", "model"})
        },
    )

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_auto_spacing",
    )

    expected = "制造企业已经拥有，MES 与 Physical World Model。"
    assert parsed.pages[0].content == expected
    assert parsed.pages[0].plain_text == expected
    warning = next(warning for warning in parsed.warnings if warning.code == "auto_text_normalized")
    assert warning.details and warning.details["pages"] == [1]

    standard_service = ParserService(
        SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False)
    )
    standard = StaticParser(source_text)
    standard_service.registry.register(ParseMode.STANDARD, standard)  # type: ignore[arg-type]
    standard_service._initialized = True
    unchanged = await standard_service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.STANDARD),
        document_id="docparse_standard_spacing",
    )
    assert unchanged.pages[0].content == source_text
    assert unchanged.pages[0].plain_text == source_text


async def test_auto_preserves_only_native_cjk_space_boundaries(monkeypatch) -> None:
    source_text = "质 量 管 理 体 系 要 求；制 造 企 业"
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False))
    primary = StaticParser(source_text)
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service._initialized = True
    monkeypatch.setattr(
        service,
        "_native_lexicons",
        lambda _source, _pages: {1: _NativePageEvidence(cjk_space_boundaries={"系要"})},
    )

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_native_cjk_boundary",
    )

    assert parsed.pages[0].content == "质量管理体系 要求；制造企业"
    assert parsed.pages[0].plain_text == "质量管理体系 要求；制造企业"


async def test_auto_flags_measured_severe_spacing_without_claiming_layout_repair(monkeypatch) -> None:
    source_text = "甲 乙 丙 丁 戊 己 庚 辛 壬 癸 子 丑 寅 卯 辰 巳 午 未 申 酉 戌 亥 天 地"
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False))
    primary = StaticParser(source_text)
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service._initialized = True
    monkeypatch.setattr(service, "_native_lexicons", lambda _source, _pages: {})

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_severe_spacing",
    )

    assert not parsed.pages[0].warnings
    assert parsed.pages[0].status.value == "completed"
    warning = next(item for item in parsed.warnings if item.code == "auto_text_normalized")
    assert warning.severity.value == "info"
    assert warning.details and warning.details["severe_spacing_pollution"] == {
        "pages": [1],
        "metrics": [
            {
                "page_number": 1,
                "removed_spaces": 23,
                "removed_to_visible_ratio": pytest.approx(23 / 24, abs=0.0001),
                "layout_order_repaired": False,
            }
        ],
    }


async def test_auto_flags_native_identifier_loss_and_numbered_heading_order(monkeypatch) -> None:
    source_text = (
        "## 3.3 Physical World Model、用例生成受控自我化测试与进\n\n"
        "证据不足时进入 d _ more _ evidence，而不是勉强判定通过。nee"
    )
    evidence = _NativePageEvidence(
        underscore_identifiers=[
            _NativeIdentifierEvidence(
                identifier="need_more_evidence",
                prefix="据不足时进入",
                suffix="而不是勉强判",
            )
        ],
        numbered_headings={"3.3": "Physical World Model、测试用例生成与受控自我进化"},
    )
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False))
    primary = StaticParser(source_text)
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service._initialized = True
    monkeypatch.setattr(service, "_native_lexicons", lambda _source, _pages: {1: evidence})

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_native_residuals",
    )

    warnings = {item.code: item for item in parsed.pages[0].warnings}
    assert set(warnings) == {"native_identifier_missing", "native_heading_order_mismatch"}
    assert warnings["native_identifier_missing"].details == {
        "identifiers": ["need_more_evidence"],
        "minimum_length": 6,
        "same_page_context_verified": True,
    }
    assert warnings["native_heading_order_mismatch"].details == {
        "mismatches": [
            {
                "section": "3.3",
                "native_heading": "Physical World Model、测试用例生成与受控自我进化",
                "parsed_heading": "Physical World Model、用例生成受控自我化测试与进",
            }
        ],
        "comparison": "same_section_same_characters_different_order_after_whitespace_removed",
    }
    assert not service.quality_service.assess(parsed).acceptable


async def test_auto_accepts_whitespace_only_native_identifier_and_heading_differences(monkeypatch) -> None:
    source_text = (
        "## 3.3 Physical World Model、 测试用例生成 与受控自我进化\n\n"
        "证据不足时进入 need _ more _ evidence，而不是勉强判定通过。"
    )
    evidence = _NativePageEvidence(
        underscore_identifiers=[
            _NativeIdentifierEvidence(
                identifier="need_more_evidence",
                prefix="据不足时进入",
                suffix="而不是勉强判",
            )
        ],
        numbered_headings={"3.3": "Physical World Model、测试用例生成与受控自我进化"},
    )
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0, vlm_enabled=False))
    primary = StaticParser(source_text)
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service._initialized = True
    monkeypatch.setattr(service, "_native_lexicons", lambda _source, _pages: {1: evidence})

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_native_residuals_whitespace_only",
    )

    codes = {item.code for item in parsed.pages[0].warnings}
    assert "native_identifier_missing" not in codes
    assert "native_heading_order_mismatch" not in codes


async def test_preserved_suspicious_glyph_remains_visible_to_quality_fallback(monkeypatch) -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser("Manufacturing private glyph \ue123 remains")
    vlm = RepairVlm("Manufacturing text recovered by VLM", backend="ollama-vlm")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.vlm_parser = vlm  # type: ignore[assignment]
    service._initialized = True
    monkeypatch.setattr(service, "_native_lexicons", lambda _source, _pages: {})

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO, enable_vlm_fallback=True),
        document_id="docparse_suspicious_glyph_fallback",
    )

    assert vlm.calls == 1
    assert not any(item.code == "suspicious_unicode_glyphs" for item in parsed.warnings)
    assert parsed.pages[0].content == "Manufacturing text recovered by VLM"
    fallback_warning = next(item for item in parsed.warnings if item.code == "vlm_fallback_used")
    assert fallback_warning.details == {
        "pages": [1],
        "resolved_warnings": [{"code": "suspicious_unicode_glyphs", "page_number": 1}],
    }


async def test_vlm_fallback_consumes_only_full_fingerprint_mirrors_as_a_multiset() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    service.vlm_parser = RepairVlm("Recovered page", backend="ollama-vlm")  # type: ignore[assignment]
    parsed = result("docparse_resolved_warnings")
    mirrored = ParseWarning(
        code="native_identifier_missing",
        message="identifier missing",
        page_number=1,
        backend="docling-standard",
        details={"identifiers": ["need_more_evidence"], "context": {"before": "进入", "after": "而不是"}},
    )
    parsed.pages[0].warnings = [mirrored]
    same_key_independent = ParseWarning(
        code=mirrored.code,
        message="independent diagnostic with the same code and page",
        page_number=mirrored.page_number,
        severity="error",
        backend="independent-checker",
        details={"identifiers": ["another_identifier"]},
    )
    other_page = mirrored.model_copy(update={"page_number": 2})
    document_level = mirrored.model_copy(update={"page_number": None})
    parsed.warnings = [
        mirrored.model_copy(
            update={
                "details": {
                    "context": {"after": "而不是", "before": "进入"},
                    "identifiers": ["need_more_evidence"],
                }
            }
        ),
        mirrored.model_copy(),
        same_key_independent,
        other_page,
        document_level,
    ]

    await service._apply_vlm_fallback(
        parsed,
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO, enable_vlm_fallback=True),
        [1],
        document_id=parsed.document_id,
        cancel_event=None,
    )

    assert parsed.pages[0].content == "Recovered page"
    fallback_warning = next(warning for warning in parsed.warnings if warning.code == "vlm_fallback_used")
    remaining = [warning for warning in parsed.warnings if warning is not fallback_warning]
    assert sum(warning == mirrored for warning in remaining) == 1
    assert same_key_independent in remaining
    assert other_page in remaining
    assert document_level in remaining
    assert len(remaining) == 4
    assert fallback_warning.details == {
        "pages": [1],
        "resolved_warnings": [{"code": "native_identifier_missing", "page_number": 1}],
    }


async def test_auto_normalizes_only_contextual_short_mapped_tokens() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser(
        "## 犌犅 ／ 犜 ４ ０ ７ ２ ７\n\n"
        "附 录 犃 （资料性）\n\n图 犃 ． １\n\n## 犇 ． １\n\n犬犃是原文"
    )
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.glm_adapter = StaticProbe(BackendState.UNAVAILABLE)  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_contextual_unicode",
    )

    content = parsed.pages[0].content or ""
    assert "## GB/T ４ ０ ７ ２ ７" in content
    assert "附录A （资料性）" in content
    assert "图A ． １" in content
    assert "## D ． １" in content
    assert "犬犃是原文" in content
    warning = next(item for item in parsed.warnings if item.code == "auto_contextual_unicode_normalized")
    assert warning.details == {"pages": [1], "strategy": "contextual_standard_and_appendix_labels"}


async def test_standard_mode_keeps_contextual_short_mapped_tokens() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser("## 犌犅 ／ 犜\n\n附 录 犃")
    service.registry.register(ParseMode.STANDARD, primary)  # type: ignore[arg-type]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.STANDARD),
        document_id="docparse_standard_contextual_unicode",
    )

    assert parsed.pages[0].content == "## 犌犅 ／ 犜\n\n附 录 犃"
    assert not any(item.code == "auto_contextual_unicode_normalized" for item in parsed.warnings)


async def test_auto_uses_opt_in_vlm_only_after_glm_repair_is_unavailable() -> None:
    service = ParserService(SimpleNamespace(parser_workers=1, document_timeout_seconds=1.0))
    primary = StaticParser("犚 犲 犿 犪 狀 狌 犳 犪 犮 狋 狌 狉 犻 狀 犵")
    ocr = StaticParser("OCR should not run", backend="glm-ocr-remote")
    vlm = RepairVlm("Remanufacturing", backend="ollama-vlm")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.standard_parser = ocr  # type: ignore[assignment]
    service.glm_adapter = StaticProbe(BackendState.UNAVAILABLE)  # type: ignore[assignment]
    service.vlm_parser = vlm  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO, enable_vlm_fallback=True),
        document_id="docparse_auto_vlm_repair",
    )

    assert parsed.pages[0].content == "Remanufacturing"
    assert ocr.calls == 0
    assert vlm.calls == 1
    assert parsed.route_summary.vlm_pages == 1
    warning = next(warning for warning in parsed.warnings if warning.code == "vlm_fallback_used")
    assert warning.details == {
        "pages": [1],
        "resolved_warnings": [{"code": "suspicious_unicode_mojibake", "page_number": 1}],
    }


async def test_auto_inserts_strict_mermaid_after_retained_picture_and_reports_phase() -> None:
    service = ParserService(
        SimpleNamespace(
            parser_workers=1,
            document_timeout_seconds=1.0,
            vlm_diagram_enrichment_enabled=True,
        )
    )
    primary = StaticParser("流程图\n\n<!-- image -->")
    primary.picture_candidates = [
        PictureCandidate(
            page_number=1,
            placeholder_index=0,
            caption="图 A.1 装配流程图",
            normalized_bbox=(0.2, 0.2, 0.8, 0.8),
        )
    ]
    vlm = DiagramVlm("```mermaid\nflowchart TD\n n1[检查] --> n2[装配]\n```")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.vlm_parser = vlm  # type: ignore[assignment]
    service._initialized = True
    progress: list[tuple[int, int, str]] = []

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_diagram",
        progress_callback=lambda current, total, phase: progress.append((current, total, phase)),
    )

    assert "<!-- image -->\n\n```mermaid" in (parsed.pages[0].content or "")
    assert len(vlm.calls) == 1
    assert parsed.pipeline.vlm == "ollama-vlm"
    assert parsed.route_summary.vlm_pages == 1
    warning = next(warning for warning in parsed.warnings if warning.code == "diagram_mermaid_generated")
    assert warning.details and warning.details["source_retained"] == "original_document_and_markdown_image_placeholder"
    assert progress == [(1, 1, "postprocess.diagram")]


async def test_auto_keeps_picture_placeholder_when_diagram_generation_fails() -> None:
    service = ParserService(
        SimpleNamespace(
            parser_workers=1,
            document_timeout_seconds=1.0,
            vlm_diagram_enrichment_enabled=True,
        )
    )
    original = "流程图\n\n<!-- image -->"
    primary = StaticParser(original)
    primary.picture_candidates = [
        PictureCandidate(
            page_number=1,
            placeholder_index=0,
            caption="图 A.1 装配流程图",
            normalized_bbox=(0.2, 0.2, 0.8, 0.8),
        )
    ]
    vlm = FailingDiagramVlm("")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.vlm_parser = vlm  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_diagram_failure",
    )

    assert parsed.pages[0].content == original
    assert any(warning.code == "diagram_enrichment_failed" for warning in parsed.warnings)


async def test_auto_diagram_enrichment_is_fail_soft_for_unexpected_exception() -> None:
    service = ParserService(
        SimpleNamespace(
            parser_workers=1,
            document_timeout_seconds=1.0,
            vlm_diagram_enrichment_enabled=True,
        )
    )
    original = "流程图\n\n<!-- image -->"
    primary = StaticParser(original)
    primary.picture_candidates = [
        PictureCandidate(
            page_number=1,
            placeholder_index=0,
            caption="图 A.1 装配流程图",
            normalized_bbox=(0.2, 0.2, 0.8, 0.8),
        )
    ]
    vlm = UnexpectedDiagramVlm("")
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.vlm_parser = vlm  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_diagram_unexpected_failure",
    )

    assert parsed.pages[0].content == original
    warning = next(warning for warning in parsed.warnings if warning.code == "diagram_enrichment_failed")
    assert warning.details and warning.details["reason"] == "RuntimeError"


async def test_auto_keeps_picture_placeholder_when_diagram_renderer_crashes() -> None:
    service = ParserService(
        SimpleNamespace(
            parser_workers=1,
            document_timeout_seconds=1.0,
            vlm_diagram_enrichment_enabled=True,
        )
    )
    primary = StaticParser("流程图\n\n<!-- image -->")
    primary.picture_candidates = [
        PictureCandidate(
            page_number=1,
            placeholder_index=0,
            caption="图 A.1 装配流程图",
            normalized_bbox=(0.2, 0.2, 0.8, 0.8),
        )
    ]
    service.registry.register(ParseMode.AUTO, primary)  # type: ignore[arg-type]
    service.vlm_parser = CrashingDiagramVlm("unused")  # type: ignore[assignment]
    service._initialized = True

    parsed = await service.parse(
        stored_source(),
        DocumentParseOptions(mode=ParseMode.AUTO),
        document_id="docparse_diagram_crash",
    )

    assert parsed.pages[0].content == "流程图\n\n<!-- image -->"
    warning = next(item for item in parsed.warnings if item.code == "diagram_enrichment_failed")
    assert warning.details == {"reason": "RuntimeError", "source_retained": "markdown_image_placeholder"}
