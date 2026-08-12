"""Persistent job status, event, result, retry, and deletion endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import orjson
from fastapi import APIRouter, Depends, Form, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import DeleteJobResponse, JobResponse
from app.models import (
    DocumentParseResult,
    JobEvent,
    JobRecord,
    JobStatus,
    OutputFormat,
    ServiceError,
)

router = APIRouter(
    prefix="/v1/documents/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


async def _job_response(request: Request, record: JobRecord) -> JobResponse:
    result: DocumentParseResult | None = None
    if record.status in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
        try:
            result = await get_runtime(request).storage_service.read_metadata(record.id)
        except FileNotFoundError:
            # The durable job error remains visible; result retrieval maps a
            # missing file to a precise error when the caller requests it.
            result = None
    return JobResponse.from_record(record, result)


@router.get("/{job_id}", response_model=JobResponse, summary="Get persistent job state")
async def get_document_job(job_id: str, request: Request) -> JobResponse:
    record = await get_runtime(request).job_service.get_job(job_id)
    return await _job_response(request, record)


def _sse(event: JobEvent) -> bytes:
    data = {"type": event.event, "job_id": event.job_id, **event.data}
    payload = orjson.dumps(data).decode("utf-8")
    return f"id: {event.id}\nevent: {event.event}\ndata: {payload}\n\n".encode()


@router.get("/{job_id}/events", summary="Stream job progress as Server-Sent Events")
async def document_job_events(
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


def _download_headers(filename: str, output_format: OutputFormat) -> dict[str, str]:
    suffix = ".md" if output_format is OutputFormat.MARKDOWN else ".txt"
    stem = Path(filename).stem or "document"
    ascii_stem = "".join(character for character in stem if character.isascii() and character.isalnum())
    fallback = f"{ascii_stem or 'document'}{suffix}"
    encoded = quote(f"{stem}{suffix}")
    return {"Content-Disposition": f"attachment; filename={fallback}; filename*=UTF-8''{encoded}"}


@router.get("/{job_id}/result", summary="Read or download completed job output")
async def get_document_result(
    job_id: str,
    request: Request,
    format: Annotated[OutputFormat, Query()] = OutputFormat.MARKDOWN,
    download: Annotated[bool, Query()] = False,
) -> Response:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    content = await service.get_result(job_id, format.value)
    media_type = "text/markdown" if format is OutputFormat.MARKDOWN else "text/plain"
    headers = _download_headers(record.filename, format) if download else {}
    return Response(content=content, media_type=media_type, headers=headers)


@router.post("/{job_id}/retry", response_model=JobResponse, summary="Retry a failed job")
async def retry_document_job(
    job_id: str,
    request: Request,
    page_range: Annotated[str | None, Form()] = None,
) -> JobResponse:
    record = await get_runtime(request).job_service.retry_job(job_id, page_range=page_range)
    return JobResponse.from_record(record)


@router.delete(
    "/{job_id}",
    response_model=DeleteJobResponse,
    summary="Cancel an active job or delete a terminal job",
)
async def delete_document_job(job_id: str, request: Request) -> DeleteJobResponse:
    service = get_runtime(request).job_service
    record = await service.get_job(job_id)
    if record.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        await service.cancel_job(job_id)
        return DeleteJobResponse(id=job_id, status="cancelled")
    await service.delete_job(job_id)
    return DeleteJobResponse(id=job_id, status="deleted")
