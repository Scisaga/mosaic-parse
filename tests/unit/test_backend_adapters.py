import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.pipeline_options import OcrMode
from PIL import Image

from app.models import BackendState, DocumentParseOptions, ParseMode, StoredSource
from app.parsers.base import ParserError
from app.parsers.glm_ocr_remote import GlmOcrRemoteAdapter, _install_plugin_compatibility
from app.parsers.ollama_vlm import OllamaVlmParser


def async_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_glm_probe_reports_real_model_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "zai-org/GLM-OCR"}]})

    client = async_client(handler)
    adapter = GlmOcrRemoteAdapter(
        SimpleNamespace(
            glm_ocr_enabled=True,
            glm_ocr_api_url="http://glm.test/v1/chat/completions",
            glm_ocr_model="zai-org/GLM-OCR",
            backend_health_ttl_seconds=0,
        ),
        client,
    )
    status = await adapter.probe()
    assert status.state == BackendState.READY
    assert status.model == "zai-org/GLM-OCR"
    assert status.latency_ms is not None
    await client.aclose()


async def test_glm_probe_distinguishes_disabled_and_unavailable() -> None:
    disabled_client = async_client(lambda _: httpx.Response(500))
    disabled = GlmOcrRemoteAdapter(SimpleNamespace(glm_ocr_enabled=False), disabled_client)
    assert (await disabled.probe()).state == BackendState.DISABLED
    await disabled_client.aclose()

    unavailable_client = async_client(lambda _: httpx.Response(503))
    unavailable = GlmOcrRemoteAdapter(
        SimpleNamespace(
            glm_ocr_enabled=True,
            glm_ocr_api_url="http://glm.test/v1/chat/completions",
            backend_health_ttl_seconds=0,
        ),
        unavailable_client,
    )
    assert (await unavailable.probe()).state == BackendState.UNAVAILABLE
    await unavailable_client.aclose()


def test_glm_plugin_options_are_mapped_without_fake_confidence() -> None:
    client = async_client(lambda _: httpx.Response(200))
    adapter = GlmOcrRemoteAdapter(
        SimpleNamespace(
            glm_ocr_enabled=True,
            glm_ocr_api_url="http://glm.test/v1/chat/completions",
            glm_ocr_model="zai-org/GLM-OCR",
            glm_ocr_max_concurrency=2,
            glm_ocr_max_retries=4,
        ),
        client,
    )
    options = adapter.build_options(
        languages=["zh", "en"],
        scale=2.5,
        force_full_page_ocr=True,
    )
    assert options.lang == ["zh", "en"]
    assert options.scale == 2.5
    assert options.mode is OcrMode.FULL_PAGE
    assert options.max_concurrent_requests == 2
    assert options.max_retries == 4
    assert options.max_tokens == 4096
    assert options.prompt == "Text Recognition:"
    assert "confidence" not in type(options).model_fields


def test_glm_plugin_sends_image_before_fixed_prompt() -> None:
    from docling_glm_ocr import GlmOcrRemoteModel, GlmOcrRemoteOptions

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.read()))
        return httpx.Response(200, json={"choices": [{"message": {"content": "complete"}}]})

    _install_plugin_compatibility()
    model = GlmOcrRemoteModel(
        enabled=True,
        artifacts_path=None,
        options=GlmOcrRemoteOptions(
            api_url="http://glm.test/v1/chat/completions",
            prompt="Text Recognition:",
            max_tokens=4096,
        ),
        accelerator_options=AcceleratorOptions(device="cpu"),
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    model._local.client = client
    try:
        assert model._recognise_crop(Image.new("RGB", (4, 4), "white")) == "complete"
    finally:
        client.close()

    content = captured["messages"][0]["content"]  # type: ignore[index]
    assert content[0]["type"] == "image_url"
    assert str(content[0]["image_url"]["url"]).startswith("data:image/png;base64,")
    assert content[1] == {"type": "text", "text": "Text Recognition:"}
    assert captured["max_tokens"] == 4096


async def test_ollama_vlm_parses_an_image_through_openai_contract() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "vision-test"}]})
        assert request.url.path == "/v1/chat/completions"
        payload = request.read().decode()
        assert "data:image/png;base64," in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "# OCR result\n\nValue: 12,345.67"}}]},
        )

    client = async_client(handler)
    parser = OllamaVlmParser(
        SimpleNamespace(
            vlm_enabled=True,
            vlm_base_url="http://ollama.test/v1",
            vlm_model="vision-test",
            vlm_api_key="test-key",
            vlm_timeout_seconds=10,
            vlm_max_concurrency=1,
            backend_health_ttl_seconds=0,
        ),
        client,
    )
    image_path = Path("tests/fixtures/sample-image.png").resolve()
    result = await parser.parse(
        StoredSource(
            path=image_path,
            filename=image_path.name,
            mime_type="image/png",
            size_bytes=image_path.stat().st_size,
            page_count=1,
        ),
        DocumentParseOptions(mode=ParseMode.VLM),
        document_id="docparse_vlmtest",
    )
    assert result.pages[0].content == "# OCR result\n\nValue: 12,345.67"
    assert result.route_summary.vlm_pages == 1
    assert result.route_summary.ocr_regions is None
    assert calls == ["/v1/models", "/v1/chat/completions"]
    await client.aclose()


def test_mermaid_validation_requires_topology_and_rejects_active_content() -> None:
    valid = "```mermaid\nflowchart TD\n  n1[检查] -->|Y| n2[装配]\n```"
    assert OllamaVlmParser.validate_mermaid(valid) == valid

    with pytest.raises(ParserError, match="no relationship edges"):
        OllamaVlmParser.validate_mermaid("```mermaid\nflowchart TD\n  n1[只有节点]\n```")
    with pytest.raises(ParserError, match="unsafe"):
        OllamaVlmParser.validate_mermaid(
            "```mermaid\nflowchart TD\n  n1[检查] --> n2[装配]\n  click n2 https://evil.test\n```"
        )
    with pytest.raises(ParserError, match="strict Mermaid"):
        OllamaVlmParser.validate_mermaid("flowchart TD\nn1 --> n2")


def test_mermaid_validation_accepts_all_declared_nodes_when_connected() -> None:
    mermaid = (
        "```mermaid\n"
        "flowchart TD\n"
        "  n1[准备零件]\n"
        "  n2{零件检验}\n"
        "  n3[再制造组件]\n"
        "  n1 --> n2\n"
        "  n2 -->|Y| n3\n"
        "  n2 -->|N| n1\n"
        "```"
    )

    assert OllamaVlmParser.validate_mermaid(mermaid) == mermaid


def test_mermaid_validation_rejects_declared_isolated_stage_annotation() -> None:
    mermaid = (
        "```mermaid\n"
        "flowchart TD\n"
        "  n1[准备零件] --> n2{零件检验}\n"
        "  n3[组件装配]\n"
        "```"
    )

    with pytest.raises(ParserError, match=r"isolated node\(s\): n3"):
        OllamaVlmParser.validate_mermaid(mermaid)
