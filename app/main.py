"""FastAPI application factory and production ASGI application."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route

from app.api import admin, backends, documents, health, jobs
from app.config import Settings, get_settings
from app.lifespan import RuntimeFactory, create_lifespan
from app.mcp.server import McpBundle, create_mcp
from app.models import ErrorBody, ErrorResponse, ServiceError
from app.security.auth import extract_bearer_token, token_matches
from app.utils.ids import new_request_id
from app.utils.logging import configure_logging

logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else new_request_id()


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
        )
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json", exclude_none=True))


def _static_directory(settings: Settings) -> Path | None:
    candidates = [settings.static_dir, Path("frontend/dist")]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return None


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
) -> FastAPI:
    """Create an isolated application, allowing lightweight integration tests."""

    current_settings = settings or get_settings()
    configure_logging(current_settings.log_level, json_logs=current_settings.json_logs)
    runtime_ref: dict[str, Any] = {}

    def current_runtime():
        runtime = runtime_ref.get("runtime")
        if runtime is None:
            raise ServiceError("service_not_ready", "The parser service is still starting", status_code=503)
        return runtime

    mcp_bundle: McpBundle | None = None
    if current_settings.mcp_enabled:
        mcp_bundle = create_mcp(current_runtime, current_settings)

    application = FastAPI(
        title="Docling GLM",
        summary="Self-hosted PDF/image to Markdown or plain-text parser",
        description=(
            "An independent community service powered by Docling with optional remote GLM-OCR "
            "and VLM backends. It does not perform metric extraction, RAG, summarization, or Q&A."
        ),
        version=current_settings.version,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=create_lifespan(
            current_settings,
            runtime_ref=runtime_ref,
            mcp_server=mcp_bundle.server if mcp_bundle else None,
            runtime_factory=runtime_factory,
        ),
    )
    application.state.settings = current_settings

    if current_settings.cors_origin_list:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=current_settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Last-Event-ID",
                "X-API-Key",
                "X-Admin-Token",
                "X-Request-ID",
                "Mcp-Method",
                "Mcp-Name",
                "Mcp-Protocol-Version",
                "Mcp-Session-Id",
            ],
            expose_headers=["Mcp-Session-Id", "X-Request-ID"],
        )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID")
        request.state.request_id = incoming if incoming and _REQUEST_ID_RE.fullmatch(incoming) else new_request_id()
        started = time.perf_counter()

        if request.url.path == "/mcp" or request.url.path.startswith("/mcp/"):
            provided = extract_bearer_token(
                request.headers.get("Authorization"),
                request.headers.get("X-API-Key"),
            )
            if not token_matches(provided, current_settings.api_key):
                return _error_response(
                    status_code=401,
                    code="invalid_api_key",
                    message="A valid API key is required",
                    request_id=request.state.request_id,
                )

        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        logger.info(
            "request completed",
            extra={
                "request_id": request.state.request_id,
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "status": response.status_code,
            },
        )
        return response

    @application.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
            details=exc.details,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()
        ]
        return _error_response(
            status_code=422,
            code="validation_error",
            message="The request parameters are invalid",
            request_id=_request_id(request),
            details={"errors": errors},
        )

    @application.exception_handler(StarletteHTTPException)
    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = str(exc.detail) if exc.detail else "HTTP request failed"
        code = "not_found" if exc.status_code == 404 else f"http_{exc.status_code}"
        return _error_response(
            status_code=exc.status_code,
            code=code,
            message=message,
            request_id=_request_id(request),
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled request error", extra={"request_id": _request_id(request)})
        return _error_response(
            status_code=500,
            code="internal_error",
            message="An unexpected internal error occurred",
            request_id=_request_id(request),
        )

    application.include_router(documents.router)
    application.include_router(jobs.router)
    application.include_router(backends.router)
    application.include_router(health.router)
    application.include_router(admin.router)

    if mcp_bundle is not None:
        # Mount strips the exact prefix to an empty child path in current
        # Starlette, which turns POST /mcp into a 405 while /mcp/ works. Reuse
        # the SDK's ASGI endpoint directly so the documented URL is exact.
        sdk_route = mcp_bundle.app.routes[0]
        if not isinstance(sdk_route, Route):
            raise RuntimeError("MCP SDK did not expose the expected Streamable HTTP route")
        mcp_endpoint = sdk_route.endpoint
        application.router.routes.extend(
            [
                Route("/mcp", endpoint=mcp_endpoint, name="mcp"),
                Route("/mcp/", endpoint=mcp_endpoint, name="mcp-trailing-slash"),
            ]
        )

    static_directory = _static_directory(current_settings)
    if static_directory is not None:
        application.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
    else:
        @application.get("/", include_in_schema=False)
        async def development_root() -> HTMLResponse:
            return HTMLResponse(
                "<h1>Docling GLM</h1><p>Frontend assets are not built. "
                '<a href="/docs">Open the API documentation</a>.</p>'
            )

    return application


app = create_app()
