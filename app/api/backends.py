"""Parser backend capability endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import BackendsResponse, QueueStatus

router = APIRouter(tags=["backends"], dependencies=[Depends(require_api_key)])


@router.get("/v1/backends", response_model=BackendsResponse, summary="Probe parser backends")
async def get_backends(request: Request) -> BackendsResponse:
    runtime = get_runtime(request)
    backends = await runtime.parser_service.probe_backends()
    return BackendsResponse(
        backends=backends,
        queue=QueueStatus(
            active=runtime.job_service.queue_depth,
            capacity=runtime.settings.max_queued_jobs,
        ),
    )
