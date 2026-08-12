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

from app.api.schemas import JobResponse, ParseResponse
from app.models import DocumentParseOptions, OutputFormat, ParseMode, ParseProfile, ServiceError

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
        "Docling GLM",
        version=settings.version,
        instructions=(
            "Convert PDF and image documents to Markdown or plain text. "
            "Use auto for ordinary PDFs, ocr for scans, and vlm only for complex visual layouts. "
            "This service does not extract financial metrics and does not provide RAG or Q&A."
        ),
    )

    @server.tool()
    async def parse_document(
        source_url: str | None = None,
        file_base64: str | None = None,
        filename: str | None = None,
        mode: str = "auto",
        profile: str = "balanced",
        output_format: str = "markdown",
        page_range: str | None = None,
        enable_vlm_fallback: bool = False,
    ) -> dict[str, object]:
        """Parse one HTTP(S) URL or one small base64 PDF/image."""

        if (source_url is None) == (file_base64 is None):
            return _service_error(
                ServiceError(
                    "invalid_source",
                    "Exactly one of source_url or file_base64 is required",
                )
            )
        try:
            options = DocumentParseOptions(
                mode=ParseMode(mode),
                profile=ParseProfile(profile),
                output_format=OutputFormat(output_format),
                page_range=page_range,
                enable_vlm_fallback=enable_vlm_fallback,
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
        force_job = content is not None and len(content) > settings.mcp_max_inline_bytes
        try:
            if force_job:
                job = await job_service.create_job(
                    content=content,
                    filename=filename,
                    options=options,
                )
                return {
                    "delivery": "job",
                    **JobResponse.from_record(job).model_dump(mode="json", exclude_none=True),
                }

            result = await job_service.parse_sync(
                source_url=source_url,
                content=content,
                filename=filename,
                options=options,
            )
            response = ParseResponse.from_result(result, options)
            if len(response.content) <= settings.mcp_max_result_chars:
                return {"delivery": "inline", **response.model_dump(mode="json", exclude_none=True)}

            # Preserve complete output through the HTTP job contract. This rare
            # branch reparses rather than silently truncating model-visible text.
            job = await job_service.create_job(
                source_url=source_url,
                content=content,
                filename=filename,
                options=options,
            )
            return {
                "delivery": "job",
                "message": "The result exceeds the MCP inline limit; use the result URL.",
                **JobResponse.from_record(job).model_dump(mode="json", exclude_none=True),
            }
        except ServiceError as exc:
            if exc.code == "sync_limit_exceeded":
                try:
                    job = await job_service.create_job(
                        source_url=source_url,
                        content=content,
                        filename=filename,
                        options=options,
                    )
                    return {
                        "delivery": "job",
                        **JobResponse.from_record(job).model_dump(mode="json", exclude_none=True),
                    }
                except ServiceError as job_exc:
                    return _service_error(job_exc)
            return _service_error(exc)

    @server.tool()
    async def get_document_job(job_id: str) -> dict[str, object]:
        """Get the durable status of a document parsing job."""

        try:
            record = await runtime().job_service.get_job(job_id)
            return JobResponse.from_record(record).model_dump(mode="json", exclude_none=True)
        except ServiceError as exc:
            return _service_error(exc)

    @server.tool()
    async def get_document_result(job_id: str, output_format: str = "markdown") -> dict[str, object]:
        """Get a completed job result when it is small enough for MCP."""

        try:
            selected = OutputFormat(output_format)
            content = await runtime().job_service.get_result(job_id, selected.value)
            if len(content) > settings.mcp_max_result_chars:
                return {
                    "job_id": job_id,
                    "delivery": "http",
                    "result_url": f"/v1/documents/jobs/{job_id}/result?format={selected.value}",
                    "message": "The result exceeds the MCP inline limit.",
                }
            return {
                "job_id": job_id,
                "delivery": "inline",
                "output_format": selected.value,
                "content": content,
            }
        except (ValueError, ServiceError) as exc:
            if isinstance(exc, ServiceError):
                return _service_error(exc)
            return _service_error(ServiceError("invalid_output_format", str(exc), status_code=422))

    @server.resource("doclingglm://health", mime_type="application/json")
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

    @server.resource("doclingglm://backends", mime_type="application/json")
    async def backends_resource() -> dict[str, object]:
        """Return actual Docling, GLM-OCR, and optional VLM probes."""

        items = await runtime().parser_service.probe_backends()
        return {"backends": [item.model_dump(mode="json", exclude_none=True) for item in items]}

    @server.resource("doclingglm://usage", mime_type="text/markdown")
    def usage_resource() -> str:
        """Describe safe routing and the service boundary."""

        return (
            "# Docling GLM usage\n\n"
            "- Use `auto` for ordinary native PDFs.\n"
            "- Use `ocr` for scans and document images when GLM-OCR is ready.\n"
            "- Use `vlm` only for difficult visual layouts when the VLM backend is enabled.\n"
            "- Inputs are HTTP(S) URLs or base64 data; local filesystem paths are rejected.\n"
            "- Output is Markdown or plain text only. Financial extraction, RAG, and Q&A are out of scope."
        )

    @server.prompt()
    def document_parse_workflow(document_kind: str = "ordinary PDF") -> str:
        """Select a conservative parse mode for a document."""

        return (
            f"Parse the {document_kind} with Docling GLM. Start with mode=auto. "
            "Use mode=ocr only when it is scanned or image-only, and mode=vlm only when "
            "the reading order or visual layout defeats the standard path. Return the text result "
            "without asking this parser to extract metrics, summarize, build RAG, or answer questions."
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

