"""Persistent job status, event, result, retry, and deletion endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import orjson
from fastapi import APIRouter, Depends, Form, Header, Query, Request, Response
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, StreamingResponse

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import DeleteJobResponse, JobResponse
from app.models import (
    AssetIR,
    ContentEvidenceIR,
    JobEvent,
    JobRecord,
    JobStatus,
    ServiceError,
)

router = APIRouter(
    prefix="/v1/content/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


async def _job_response(request: Request, record: JobRecord) -> JobResponse:
    return JobResponse.from_record(record)


@router.get("/{job_id}", response_model=JobResponse, summary="Get persistent job state")
async def get_content_job(job_id: str, request: Request) -> JobResponse:
    record = await get_runtime(request).job_service.get_job(job_id)
    return await _job_response(request, record)


def _sse(event: JobEvent) -> bytes:
    data = {"type": event.event, "job_id": event.job_id, **event.data}
    payload = orjson.dumps(data).decode("utf-8")
    return f"id: {event.id}\nevent: {event.event}\ndata: {payload}\n\n".encode()


@router.get("/{job_id}/events", summary="Stream job progress as Server-Sent Events")
async def content_job_events(
    job_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    parsed_last_id: int | None = None
    if last_event_id:
        try:
            parsed_last_id = int(last_event_id)
        except ValueError as exc:
            raise ServiceError(
                "invalid_last_event_id",
                "Last-Event-ID must be an integer",
                status_code=400,
            ) from exc

    service = get_runtime(request).job_service
    await service.get_job(job_id)

    async def stream():
        async for event in service.events(job_id, last_event_id=parsed_last_id):
            if await request.is_disconnected():
                break
            yield _sse(event)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _download_headers(filename: str, suffix: str) -> dict[str, str]:
    stem = Path(filename).stem or "content"
    ascii_stem = "".join(
        character for character in stem if character.isascii() and character.isalnum()
    )
    fallback = f"{ascii_stem or 'content'}{suffix}"
    encoded = quote(f"{stem}{suffix}")
    return {"Content-Disposition": f"attachment; filename={fallback}; filename*=UTF-8''{encoded}"}


@router.get(
    "/{job_id}/result",
    response_model=ContentEvidenceIR,
    summary="Read the completed content-evidence IR",
)
async def get_content_result(
    job_id: str,
    request: Request,
    download: Annotated[bool, Query()] = False,
) -> ContentEvidenceIR | Response:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    result = await service.get_evidence(job_id)
    if not download:
        return result
    return Response(
        content=result.model_dump_json(indent=2),
        media_type="application/json",
        headers=_download_headers(record.filename, ".json"),
    )


@router.get("/{job_id}/rendering/{format}", summary="Read a derived content rendering")
async def get_content_rendering(
    job_id: str,
    request: Request,
    format: Annotated[Literal["markdown", "text"], ApiPath()],
    download: Annotated[bool, Query()] = False,
) -> Response:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    content = await service.get_result(job_id, format)
    markdown = format == "markdown"
    media_type = "text/markdown" if markdown else "text/plain"
    suffix = ".md" if markdown else ".txt"
    headers = _download_headers(record.filename, suffix) if download else {}
    return Response(content=content, media_type=media_type, headers=headers)


@router.post("/{job_id}/retry", response_model=JobResponse, summary="Retry a failed job")
async def retry_content_job(
    job_id: str,
    request: Request,
    unit_range: Annotated[str | None, Form()] = None,
) -> JobResponse:
    record = await get_runtime(request).job_service.retry_job(job_id, unit_range=unit_range)
    return JobResponse.from_record(record)


@router.delete(
    "/{job_id}",
    response_model=DeleteJobResponse,
    summary="Cancel an active job or delete a terminal job",
)
async def delete_content_job(job_id: str, request: Request) -> DeleteJobResponse:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    if record.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        await service.cancel_job(job_id)
        return DeleteJobResponse(id=job_id, status="cancelled")
    await service.delete_job(job_id)
    return DeleteJobResponse(id=job_id, status="deleted")


@router.get("/{job_id}/assets", response_model=list[AssetIR], summary="List content assets")
async def get_content_assets(job_id: str, request: Request) -> list[AssetIR]:
    return (await get_runtime(request).job_service.get_evidence(job_id)).assets


def _parse_range(value: str, size: int) -> tuple[int, int]:
    if not value.startswith("bytes=") or "," in value:
        raise ServiceError("invalid_range", "only one byte range is supported", status_code=416)
    start_text, separator, end_text = value[6:].partition("-")
    if not separator:
        raise ServiceError("invalid_range", "invalid HTTP byte range", status_code=416)
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix = int(end_text)
            start = max(0, size - suffix)
            end = size - 1
    except ValueError as exc:
        raise ServiceError("invalid_range", "invalid HTTP byte range", status_code=416) from exc
    if start < 0 or end < start or start >= size:
        raise ServiceError(
            "range_not_satisfiable",
            "requested byte range is outside the asset",
            status_code=416,
            details={"size_bytes": size},
        )
    return start, min(end, size - 1)


@router.get("/{job_id}/assets/{asset_id}", summary="Download a content asset")
async def get_content_asset(job_id: str, asset_id: str, request: Request) -> Response:
    service = get_runtime(request).job_service
    evidence = await service.get_evidence(job_id)
    asset = next((item for item in evidence.assets if item.asset_id == asset_id), None)
    if asset is None:
        raise ServiceError("asset_not_found", "asset does not exist", status_code=404)
    try:
        path = service.storage.asset_path(job_id, asset_id)
    except ValueError as exc:
        raise ServiceError("invalid_asset_id", "invalid asset identifier", status_code=400) from exc
    if path is None:
        raise ServiceError("asset_missing", "persisted asset file is missing", status_code=500)
    range_header = request.headers.get("range")
    headers = {
        **_download_headers(asset.filename, ""),
        "Accept-Ranges": "bytes" if asset.kind.value == "video" else "none",
        "ETag": f'"{asset.sha256}"',
    }
    if asset.kind.value != "video" or range_header is None:
        return FileResponse(
            path, media_type=asset.mime_type, filename=asset.filename, headers=headers
        )
    start, end = _parse_range(range_header, asset.size_bytes)

    def stream():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{asset.size_bytes}",
            "Content-Length": str(end - start + 1),
        }
    )
    return StreamingResponse(stream(), status_code=206, media_type=asset.mime_type, headers=headers)


@router.get("/{job_id}/bundle", summary="Download the content evidence bundle")
async def get_content_bundle(job_id: str, request: Request) -> FileResponse:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    if record.status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
        raise ServiceError("result_not_ready", "job result is not available", status_code=409)
    await service.get_evidence(job_id)
    path = await service.storage.build_bundle(job_id)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{Path(record.filename).stem or 'content'}-assets.zip",
    )
