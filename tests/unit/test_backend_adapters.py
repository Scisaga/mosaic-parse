import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.pipeline_options import OcrMode
from PIL import Image
from pydantic import BaseModel

from app.models import BackendState
from app.parsers.glm_ocr_remote import GlmOcrRemoteAdapter, _install_plugin_compatibility
from app.parsers.ollama_vlm import (
    OllamaVisualAdapter,
    VlmEmptyContentError,
    VlmResponseTruncatedError,
    VlmSchemaError,
)


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


class _VisualAnswer(BaseModel):
    title: str
    value: str


async def test_ollama_vlm_structured_multi_image_contract() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "vision-test"}]})
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.read())
        images = [item for item in payload["messages"][0]["content"] if item["type"] == "image_url"]
        assert len(images) == 2
        assert all(item["image_url"]["url"].startswith("data:image/png;base64,") for item in images)
        assert payload["reasoning_effort"] == "low"
        assert payload["max_tokens"] == 16_384
        assert payload["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"title":"资产负债表","value":"12,345.67"}',
                            "reasoning": "private chain of thought",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    client = async_client(handler)
    parser = OllamaVisualAdapter(
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
    image = image_path.read_bytes()
    completion = await parser.complete_structured(
        [image, image],
        "Read visible values",
        _VisualAnswer,
        max_tokens=16_384,
        reasoning_effort="low",
    )
    assert completion.value.value == "12,345.67"
    assert completion.finish_reason == "stop"
    assert completion.reasoning_characters == len("private chain of thought")
    assert calls == ["/v1/chat/completions"]
    await client.aclose()


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (
            {"choices": [{"finish_reason": "length", "message": {"content": ""}}]},
            VlmResponseTruncatedError,
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]},
            VlmEmptyContentError,
        ),
        (
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"title":1}'}}]},
            VlmSchemaError,
        ),
    ],
)
async def test_ollama_vlm_models_protocol_failures(response, error_type) -> None:
    client = async_client(lambda _request: httpx.Response(200, json=response))
    parser = OllamaVisualAdapter(
        SimpleNamespace(
            vlm_enabled=True,
            vlm_base_url="http://ollama.test/v1",
            vlm_model="vision-test",
            vlm_max_retries=0,
        ),
        client,
    )

    with pytest.raises(error_type):
        await parser.complete_structured(
            [_png_bytes()],
            "Read",
            _VisualAnswer,
            max_tokens=100,
            reasoning_effort="low",
        )
    await client.aclose()


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(output, format="PNG")
    return output.getvalue()
