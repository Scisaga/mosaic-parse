"""MCP tools, resources, and prompt mounted through Streamable HTTP."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from app.api.schemas import JobResponse, PublicContentAsset
from app.models import (
    ContentParseOptions,
    JobRecord,
    ScanPolicy,
    ServiceError,
)

if TYPE_CHECKING:
    from app.config import Settings
    from app.lifespan import Runtime


@dataclass(slots=True)
class McpBundle:
    server: MCPServer[Any]
    app: Starlette


def _service_error(error: ServiceError) -> dict[str, object]:
    return {"error": error.to_response().error.model_dump(mode="json", exclude_none=True)}


def create_mcp(runtime: Callable[[], Runtime], settings: Settings) -> McpBundle:
    """Build the MCP server once so its session manager shares app lifespan."""

    server: MCPServer[Any] = MCPServer(
        "MosaicParse",
        version=settings.version,
        instructions=(
            "Parse PDF, DOCX, PPTX, images, and standalone videos into GFM Markdown. "
            "Headings, lists, paragraphs, and tables preserve the parser's visible structure; "
            "assets remain authenticated HTTP resources. Entity, relation, event, and financial "
            "fact extraction remain downstream."
        ),
    )

    @server.tool()
    async def parse_content(
        source_url: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        scan_policy: str = "auto",
        unit_range: str | None = None,
        language: str = "zh,en",
        describe_images: bool = False,
        description_language: str = "zh-CN",
        timeout_seconds: int | None = None,
        prefer_async: bool = False,
    ) -> dict[str, object]:
        """Parse one HTTP(S) URL or one base64 document, image, or video."""

        if (source_url is None) == (file_base64 is None):
            return _service_error(
                ServiceError(
                    "invalid_source",
                    "Exactly one of source_url or file_base64 is required",
                )
            )
        if scan_policy not in {"auto", "skip"}:
            return _service_error(
                ServiceError(
                    "invalid_options",
                    "scan_policy must be auto or skip",
                    status_code=422,
                )
            )
        try:
            options = ContentParseOptions(
                scan_policy=ScanPolicy(scan_policy),
                unit_range=unit_range,
                language=[item.strip() for item in language.split(",") if item.strip()],
                describe_images=describe_images,
                description_language=description_language,  # type: ignore[arg-type]
                timeout_seconds=timeout_seconds,
            )
        except (ValueError, TypeError) as exc:
            return _service_error(ServiceError("invalid_options", str(exc), status_code=422))

        content: bytes | None = None
        if file_base64 is not None:
            if not filename:
                return _service_error(
                    ServiceError("filename_required", "filename is required with file_base64")
                )
            try:
                content = base64.b64decode(file_base64, validate=True)
            except (binascii.Error, ValueError):
                return _service_error(ServiceError("invalid_base64", "file_base64 is invalid"))
            if not content:
                return _service_error(ServiceError("empty_file", "The supplied file is empty"))

        job_service = runtime().job_service
        try:
            result = await job_service.parse_content(
                source_url=source_url,
                content=content,
                filename=filename,
                options=options,
                prefer_async=(
                    prefer_async
                    or bool(content is not None and len(content) > settings.mcp_max_inline_bytes)
                ),
            )
            if isinstance(result, JobRecord):
                return {
                    "delivery": "job",
                    **JobResponse.from_record(result).model_dump(mode="json", exclude_none=True),
                }
            if len(result.markdown) <= settings.mcp_max_result_chars:
                return {
                    "delivery": "inline",
                    "content_id": result.document_id,
                    "media_type": "text/markdown",
                    "content": result.markdown,
                }

            job = await job_service.get_job(result.document_id)
            return {
                "delivery": "job",
                "message": "Markdown exceeds the MCP inline limit; use the result URL.",
                **JobResponse.from_record(job).model_dump(mode="json", exclude_none=True),
            }
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_content_job(job_id: str) -> dict[str, object]:
        """Get the durable status of a content parsing job."""

        try:
            record = await runtime().job_service.get_job(job_id)
            return JobResponse.from_record(record).model_dump(mode="json", exclude_none=True)
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_content_result(job_id: str) -> dict[str, object]:
        """Get completed Markdown when small enough for MCP."""

        try:
            content = await runtime().job_service.get_result(job_id)
            if len(content) > settings.mcp_max_result_chars:
                return {
                    "job_id": job_id,
                    "delivery": "http",
                    "media_type": "text/markdown",
                    "result_url": f"/v1/content/jobs/{job_id}/result",
                    "message": "Markdown exceeds the MCP inline limit.",
                }
            return {
                "delivery": "inline",
                "job_id": job_id,
                "media_type": "text/markdown",
                "content": content,
            }
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_content_assets(job_id: str) -> dict[str, object]:
        """List asset metadata and authenticated HTTP download URLs without base64 data."""

        try:
            assets = await runtime().job_service.get_assets(job_id)
            return {
                "job_id": job_id,
                "assets": [
                    PublicContentAsset.from_asset(asset).model_dump(mode="json", exclude_none=True)
                    for asset in assets
                ],
                "bundle_url": f"/v1/content/jobs/{job_id}/bundle",
            }
        except ServiceError as exc:
            return _service_error(exc)

    @server.resource("mosaicparse://health", mime_type="application/json")
    async def health_resource() -> dict[str, object]:
        """Return process and queue health without secrets."""

        service = runtime()
        return {
            "status": "ok",
            "version": service.settings.version,
            "uptime_seconds": service.uptime_seconds,
            "queue_depth": service.job_service.queue_depth,
            "queue_capacity": service.settings.max_queued_jobs,
        }

    @server.resource("mosaicparse://backends", mime_type="application/json")
    async def backends_resource() -> dict[str, object]:
        """Return actual Docling, GLM-OCR, and optional VLM probes."""

        items = await runtime().parser_service.probe_backends()
        return {"backends": [item.model_dump(mode="json", exclude_none=True) for item in items]}

    @server.resource("mosaicparse://usage", mime_type="text/markdown")
    def usage_resource() -> str:
        """Describe safe routing and the service boundary."""

        return (
            "# MosaicParse usage\n\n"
            "- Scanned PDF pages are processed adaptively by default (`scan_policy=auto`).\n"
            "- Use `scan_policy=skip` only to retain scanned pages as assets without OCR or table extraction.\n"
            "- Inputs are HTTP(S) URLs or base64 data; local filesystem paths are rejected.\n"
            "- The only document result is GFM Markdown (`text/markdown`).\n"
            "- Tables remain Markdown tables; entity, relation, event, and financial extraction is downstream.\n"
            "- Large media assets are authenticated HTTP downloads, never inline base64.\n"
            "- Domain entity, relation, and event extraction remains downstream."
        )

    @server.prompt()
    def content_parse_workflow(content_kind: str = "ordinary PDF") -> str:
        """Parse content with automatic scanned-page routing."""

        return (
            f"Parse the {content_kind} with MosaicParse using scan_policy=auto. Return GFM "
            "Markdown while preserving headings, lists, paragraphs, units, and logical tables. "
            "Use scan_policy=skip only when scanned PDF pages must remain unprocessed assets."
        )

    transport_security = TransportSecuritySettings(
        allowed_hosts=settings.mcp_allowed_host_list,
        allowed_origins=settings.mcp_allowed_origin_list,
    )
    app = server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        max_request_body_size=settings.mcp_max_inline_bytes * 2,
        transport_security=transport_security,
        host=settings.host,
    )
    return McpBundle(server=server, app=app)
