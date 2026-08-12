"""Administrative converter reload and retention cleanup endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import get_runtime, require_admin_token
from app.api.schemas import CleanupResponse, ReloadResponse

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_token)],
)


@router.post("/reload", response_model=ReloadResponse, summary="Rebuild parser adapters")
async def reload_parsers(request: Request) -> ReloadResponse:
    runtime = get_runtime(request)
    await runtime.parser_service.reload()
    return ReloadResponse(backends=await runtime.parser_service.probe_backends())


@router.post("/cleanup", response_model=CleanupResponse, summary="Delete expired jobs")
async def cleanup_jobs(request: Request) -> CleanupResponse:
    deleted = await get_runtime(request).cleanup_expired()
    return CleanupResponse(deleted_jobs=deleted)

