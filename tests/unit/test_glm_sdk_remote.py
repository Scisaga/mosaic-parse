from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.models import (
    BackendState,
    ContentParseOptions,
    StoredSource,
)
from app.parsers.glm_sdk_remote import GlmSdkRemoteParser
from app.services.table_service import TableFragment


def _source(path: Path, *, page_count: int = 1) -> StoredSource:
    return StoredSource(
        path=path,
        filename=path.name,
        mime_type="application/pdf",
        size_bytes=path.stat().st_size,
        page_count=page_count,
    )


async def test_sdk_adapter_renders_region_json_and_table_spans(native_pdf: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            200,
            json={
                "layout_details": [
                    [
                        {
                            "index": 0,
                            "label": "text",
                            "native_label": "doc_title",
                            "content": "测试报告",
                            "bbox_2d": [10, 10, 990, 80],
                        },
                        {
                            "index": 1,
                            "label": "table",
                            "native_label": "table",
                            "content": (
                                "<table><tr><th rowspan='2'>项目</th><th colspan='2'>金额</th></tr>"
                                "<tr><th>本期</th><th>上期</th></tr>"
                                "<tr><td>收入</td><td>100</td><td>90</td></tr></table>"
                            ),
                            "bbox_2d": [50, 100, 950, 800],
                        },
                        {
                            "index": 2,
                            "label": "image",
                            "native_label": "seal",
                            "content": "",
                            "bbox_2d": [700, 800, 950, 980],
                        },
                    ]
                ],
                "md_results": "unused",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = GlmSdkRemoteParser(
        SimpleNamespace(
            glm_sdk_enabled=True,
            glm_sdk_url="http://sdk:5002/glmocr/parse",
            glm_sdk_max_retries=0,
            glm_sdk_max_concurrency=1,
        ),
        http_client=client,
    )
    assert (await parser.probe()).state == BackendState.READY

    result = await parser.parse(
        _source(native_pdf),
        ContentParseOptions(unit_range="1"),
        document_id="doc_sdk_test",
    )

    assert result.pages[0].backend == "glm-sdk-remote"
    assert result.pages[0].content is not None
    assert result.pages[0].content.startswith("# 测试报告")
    assert "<!-- image -->" in result.pages[0].content
    assert len(result._table_fragments) == 1
    table = result._table_fragments[0]
    assert isinstance(table, TableFragment)
    assert table.rows == [
        ["项目", "金额", ""],
        ["", "本期", "上期"],
        ["收入", "100", "90"],
    ]
    assert table.has_column_header is True
    assert result.route_summary.ocr_regions == 3
    await client.aclose()


async def test_sdk_adapter_timeout_stops_later_page_calls(native_pdf: Path, monkeypatch) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"layout_details": [[]]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    parser = GlmSdkRemoteParser(
        SimpleNamespace(
            glm_sdk_enabled=True,
            glm_sdk_url="http://sdk:5002/glmocr/parse",
            glm_sdk_timeout_seconds=0.01,
            glm_sdk_max_retries=1,
            glm_sdk_max_concurrency=1,
        ),
        http_client=client,
    )
    monkeypatch.setattr(
        parser, "_render_page", lambda source, page_number: "data:image/png;base64,eA=="
    )

    result = await parser.parse(
        _source(native_pdf, page_count=2),
        ContentParseOptions(unit_range="1-2"),
        document_id="doc_sdk_timeout",
    )

    assert calls == 1
    assert result.route_summary.failed_pages == 2
    assert result.pages[0].duration_ms == 10
    assert [warning.code for warning in result.pages[0].warnings] == ["glm_sdk_timeout"]
    assert result.pages[1].duration_ms == 0
    assert [warning.code for warning in result.pages[1].warnings] == [
        "glm_sdk_skipped_after_timeout"
    ]
    await client.aclose()
