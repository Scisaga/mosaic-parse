from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient
from mcp import Client

from app.main import create_app
from app.mcp.server import create_mcp
from tests.conftest import fake_runtime, make_test_settings


async def test_mcp_tools_resources_and_prompt(tmp_path: Path, native_pdf: Path) -> None:
    settings = make_test_settings(tmp_path, mcp_enabled=True)
    runtime = fake_runtime(settings)
    await runtime.start()
    try:
        bundle = create_mcp(lambda: runtime, settings)
        async with Client(bundle.server) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "parse_document",
                "get_document_job",
                "get_document_result",
            }

            result = await client.call_tool(
                "parse_document",
                {
                    "file_base64": base64.b64encode(native_pdf.read_bytes()).decode(),
                    "filename": "native-report.pdf",
                    "mode": "auto",
                    "output_format": "markdown",
                },
            )
            assert result.is_error is False
            assert result.structured_content is not None
            assert result.structured_content["delivery"] == "inline"
            assert "12,345.67" in result.structured_content["content"]

            invalid = await client.call_tool(
                "parse_document",
                {"source_url": "https://8.8.8.8/report.pdf", "file_base64": "QQ=="},
            )
            assert invalid.structured_content is not None
            assert invalid.structured_content["error"]["code"] == "invalid_source"

            resources = await client.list_resources()
            assert {str(item.uri) for item in resources.resources} == {
                "doclingglm://health",
                "doclingglm://backends",
                "doclingglm://usage",
            }
            health = await client.read_resource("doclingglm://health")
            assert "queue_capacity" in health.contents[0].text

            prompts = await client.list_prompts()
            assert [prompt.name for prompt in prompts.prompts] == ["document_parse_workflow"]
            prompt = await client.get_prompt(
                "document_parse_workflow",
                {"document_kind": "scanned statement"},
            )
            assert "mode=ocr" in prompt.messages[0].content.text
    finally:
        await runtime.close()


async def test_mcp_large_input_returns_durable_job(tmp_path: Path, native_pdf: Path) -> None:
    settings = make_test_settings(
        tmp_path,
        mcp_enabled=True,
        mcp_max_inline_bytes=10,
    )
    runtime = fake_runtime(settings)
    await runtime.start()
    try:
        bundle = create_mcp(lambda: runtime, settings)
        async with Client(bundle.server) as client:
            result = await client.call_tool(
                "parse_document",
                {
                    "file_base64": base64.b64encode(native_pdf.read_bytes()).decode(),
                    "filename": "native-report.pdf",
                },
            )
            assert result.structured_content is not None
            assert result.structured_content["delivery"] == "job"
            assert result.structured_content["id"].startswith("job_")
            assert result.structured_content["result_url"].endswith("/result")
    finally:
        await runtime.close()


def test_streamable_http_is_exactly_mounted_at_mcp_and_uses_api_key(tmp_path: Path) -> None:
    settings = make_test_settings(
        tmp_path,
        mcp_enabled=True,
        api_key="mcp-secret",
    )
    app = create_app(settings, runtime_factory=fake_runtime)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "integration-test", "version": "1"},
        },
    }
    accept = "application/json, text/event-stream"
    with TestClient(app, base_url="http://localhost") as client:
        denied = client.post("/mcp", json=initialize, headers={"Accept": accept})
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "invalid_api_key"

        response = client.post(
            "/mcp",
            json=initialize,
            headers={"Accept": accept, "X-API-Key": "mcp-secret"},
        )
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "Docling GLM"
