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

from app.api.schemas import JobResponse
from app.models import (
    ContentParseOptions,
    JobRecord,
    ParseProfile,
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
            "Parse PDF, DOCX, PPTX, images, and standalone videos into content-evidence IR. "
            "Markdown and text are RAG-friendly projections; assets remain authenticated HTTP resources. "
            "Embedding, chunking, indexing, question answering, and domain extraction are downstream."
        ),
    )

    @server.tool()
    async def parse_content(
        source_url: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        profile: str = "balanced",
        unit_range: str | None = None,
        language: str = "zh,en",
        description_language: str = "zh-CN",
        include_renderings: bool = True,
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
        try:
            options = ContentParseOptions(
                profile=ParseProfile(profile),
                unit_range=unit_range,
                language=[item.strip() for item in language.split(",") if item.strip()],
                description_language=description_language,  # type: ignore[arg-type]
                include_renderings=include_renderings,
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
            response = result.evidence_ir
            if response is None:
                return _service_error(
                    ServiceError("ir_missing", "content evidence IR was not produced")
                )
            serialized = response.model_dump_json()
            if len(serialized) <= settings.mcp_max_result_chars:
                return {
                    "delivery": "inline",
                    **response.model_dump(mode="json", exclude_none=True),
                }

            job = await job_service.get_job(response.source.content_id)
            return {
                "delivery": "job",
                "message": "The result exceeds the MCP inline limit; use the result URL.",
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
    async def get_content_evidence(job_id: str) -> dict[str, object]:
        """Get completed content-evidence IR when small enough for MCP."""

        try:
            evidence = await runtime().job_service.get_evidence(job_id)
            if len(evidence.model_dump_json()) > settings.mcp_max_result_chars:
                return {
                    "job_id": job_id,
                    "delivery": "http",
                    "result_url": f"/v1/content/jobs/{job_id}/result",
                    "message": "The evidence IR exceeds the MCP inline limit.",
                }
            return {
                "delivery": "inline",
                **evidence.model_dump(mode="json", exclude_none=True),
            }
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_content_rendering(job_id: str, rendering: str = "markdown") -> dict[str, object]:
        """Get one derived Markdown or plain-text rendering."""

        if rendering not in {"markdown", "text"}:
            return _service_error(
                ServiceError(
                    "invalid_rendering", "rendering must be markdown or text", status_code=422
                )
            )
        try:
            content = await runtime().job_service.get_result(job_id, rendering)
            if len(content) > settings.mcp_max_result_chars:
                return {
                    "job_id": job_id,
                    "delivery": "http",
                    "result_url": f"/v1/content/jobs/{job_id}/rendering/{rendering}",
                    "message": "The rendering exceeds the MCP inline limit.",
                }
            return {
                "job_id": job_id,
                "delivery": "inline",
                "rendering": rendering,
                "content": content,
            }
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_content_assets(job_id: str) -> dict[str, object]:
        """List asset metadata and authenticated HTTP download URLs without base64 data."""

        try:
            evidence = await runtime().job_service.get_evidence(job_id)
            return {
                "job_id": job_id,
                "assets": [
                    asset.model_dump(mode="json", exclude_none=True) for asset in evidence.assets
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
            "- Use `profile=balanced` for ordinary documents and low latency.\n"
            "- Use `profile=accurate` when complex layouts need visual fusion.\n"
            "- Inputs are HTTP(S) URLs or base64 data; local filesystem paths are rejected.\n"
            "- The primary output is content-evidence IR; Markdown and text are projections.\n"
            "- Large media assets are authenticated HTTP downloads, never inline base64.\n"
            "- Entity, fact, relation, and event extraction are out of scope."
        )

    @server.prompt()
    def content_parse_workflow(content_kind: str = "ordinary PDF") -> str:
        """Select a parsing profile for content."""

        return (
            f"Parse the {content_kind} with MosaicParse. Use profile=balanced for ordinary "
            "documents or profile=accurate for complex visual evidence. Return content-evidence "
            "IR without asking this parser to embed, index, or extract domain facts."
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
