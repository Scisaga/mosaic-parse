"""Unauthenticated liveness and readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.api.dependencies import get_runtime
from app.api.schemas import HealthResponse, QueueStatus, ReadyCheck, ReadyResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
async def health(request: Request) -> HealthResponse:
    runtime = get_runtime(request)
    backends = await runtime.parser_service.probe_backends()
    docling_ready = any(item.name == "docling-standard" and item.ready for item in backends)
    return HealthResponse(
        status="ok" if docling_ready else "degraded",
        service=runtime.settings.app_id,
        version=runtime.settings.version,
        uptime_seconds=runtime.uptime_seconds,
        queue=QueueStatus(
            active=runtime.job_service.queue_depth,
            capacity=runtime.settings.max_queued_jobs,
        ),
        config=runtime.settings.public_config(),
        backends=backends,
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={503: {"model": ReadyResponse, "description": "Core parser is not ready"}},
    summary="Storage and parser readiness",
)
async def ready(request: Request, response: Response) -> ReadyResponse:
    runtime = get_runtime(request)
    checks, backends = await runtime.ready_checks()
    is_ready = all(checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        ready=is_ready,
        status="ready" if is_ready else "not_ready",
        checks=ReadyCheck(**checks),
        backends=backends,
    )

