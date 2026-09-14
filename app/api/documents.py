"""Multimodal content parsing and job creation endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import JobResponse
from app.models import (
    ContentParseOptions,
    JobRecord,
    ScanPolicy,
    ServiceError,
)

router = APIRouter(
    prefix="/v1/content",
    tags=["content"],
    dependencies=[Depends(require_api_key)],
)

_REMOVED_OPTION_FIELDS = frozenset(
    {
        "mode",
        "output_format",
        "vlm_policy",
        "enable_vlm_fallback",
        "preserve_page_breaks",
        "include_pages",
        "include_diagnostics",
        "include_renderings",
        "profile",
    }
)


async def _reject_removed_options(request: Request) -> None:
    form = await request.form()
    removed = sorted(_REMOVED_OPTION_FIELDS.intersection(form.keys()))
    if removed:
        raise ServiceError(
            "removed_options",
            "Legacy parsing options are not supported",
            status_code=422,
            details={"fields": removed},
        )


def _options(
    *,
    scan_policy: Literal["auto", "skip"],
    unit_range: str | None,
    language: str,
    describe_images: bool,
    description_language: Literal["zh-CN", "en", "auto"],
    timeout_seconds: int | None,
) -> ContentParseOptions:
    try:
        return ContentParseOptions(
            scan_policy=ScanPolicy(scan_policy),
            unit_range=unit_range,
            language=[item.strip() for item in language.split(",") if item.strip()],
            describe_images=describe_images,
            description_language=description_language,
            timeout_seconds=timeout_seconds,
        )
    except ValidationError as exc:
        errors = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()
        ]
        raise ServiceError(
            "validation_error",
            "The parsing options are invalid",
            status_code=422,
            details={"errors": errors},
        ) from exc


def _validate_source(file: UploadFile | None, source_url: str | None) -> None:
    if (file is None) == (not source_url):
        raise ServiceError(
            "invalid_source",
            "Exactly one of file or source_url must be provided",
            status_code=400,
        )


@router.post(
    "/parse",
    response_model=None,
    response_class=Response,
    summary="Parse content synchronously or automatically create an asynchronous job",
    responses={
        200: {"content": {"text/markdown": {}}, "description": "GFM Markdown"},
        202: {"model": JobResponse, "description": "Asynchronous job"},
    },
)
async def parse_content(
    request: Request,
    file: Annotated[
        UploadFile | None, File(description="Supported document, image, or video")
    ] = None,
    source_url: Annotated[str | None, Form(description="HTTP(S) content URL")] = None,
    scan_policy: Annotated[
        Literal["auto", "skip"],
        Form(description="Automatically process scanned PDF pages or skip them"),
    ] = "auto",
    unit_range: Annotated[
        str | None, Form(description="One-based page or slide ranges, e.g. 1-5,8")
    ] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    describe_images: Annotated[
        bool,
        Form(description="Generate model descriptions for embedded images"),
    ] = False,
    description_language: Annotated[Literal["zh-CN", "en", "auto"], Form()] = "zh-CN",
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
    prefer_async: Annotated[bool, Form()] = False,
) -> Response:
    await _reject_removed_options(request)
    _validate_source(file, source_url)
    options = _options(
        scan_policy=scan_policy,
        unit_range=unit_range,
        language=language,
        describe_images=describe_images,
        description_language=description_language,
        timeout_seconds=timeout_seconds,
    )
    runtime = get_runtime(request)
    result = await runtime.job_service.parse_content(
        file=file,
        source_url=source_url,
        options=options,
        prefer_async=prefer_async,
    )
    if isinstance(result, JobRecord):
        payload = JobResponse.from_record(result)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=payload.model_dump(mode="json", exclude_none=True),
        )
    return Response(
        content=result.markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Content-ID": result.document_id,
            "Location": f"/v1/content/jobs/{result.document_id}/result",
        },
    )


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an asynchronous parsing job",
)
async def create_content_job(
    request: Request,
    file: Annotated[
        UploadFile | None, File(description="Supported document, image, or video")
    ] = None,
    source_url: Annotated[str | None, Form(description="HTTP(S) content URL")] = None,
    scan_policy: Annotated[
        Literal["auto", "skip"],
        Form(description="Automatically process scanned PDF pages or skip them"),
    ] = "auto",
    unit_range: Annotated[
        str | None, Form(description="One-based page or slide ranges, e.g. 1-5,8")
    ] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    describe_images: Annotated[
        bool,
        Form(description="Generate model descriptions for embedded images"),
    ] = False,
    description_language: Annotated[Literal["zh-CN", "en", "auto"], Form()] = "zh-CN",
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
) -> JobResponse:
    await _reject_removed_options(request)
    _validate_source(file, source_url)
    options = _options(
        scan_policy=scan_policy,
        unit_range=unit_range,
        language=language,
        describe_images=describe_images,
        description_language=description_language,
        timeout_seconds=timeout_seconds,
    )
    job = await get_runtime(request).job_service.create_job(
        file=file,
        source_url=source_url,
        options=options,
    )
    return JobResponse.from_record(job)
