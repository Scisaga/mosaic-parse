"""Multimodal content parsing and job creation endpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import ValidationError

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import JobResponse, ParseResponse
from app.models import (
    ContentParseOptions,
    JobRecord,
    ParseProfile,
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
    profile: ParseProfile,
    unit_range: str | None,
    language: str,
    description_language: Literal["zh-CN", "en", "auto"],
    include_renderings: bool,
    timeout_seconds: int | None,
) -> ContentParseOptions:
    try:
        return ContentParseOptions(
            profile=profile,
            unit_range=unit_range,
            language=[item.strip() for item in language.split(",") if item.strip()],
            description_language=description_language,
            include_renderings=include_renderings,
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
    response_model=ParseResponse | JobResponse,
    summary="Parse content synchronously or automatically create an asynchronous job",
)
async def parse_content(
    request: Request,
    response: Response,
    file: Annotated[
        UploadFile | None, File(description="Supported document, image, or video")
    ] = None,
    source_url: Annotated[str | None, Form(description="HTTP(S) content URL")] = None,
    profile: Annotated[ParseProfile, Form()] = ParseProfile.BALANCED,
    unit_range: Annotated[
        str | None, Form(description="One-based page or slide ranges, e.g. 1-5,8")
    ] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    description_language: Annotated[Literal["zh-CN", "en", "auto"], Form()] = "zh-CN",
    include_renderings: Annotated[bool, Form()] = True,
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
    prefer_async: Annotated[bool, Form()] = False,
) -> ParseResponse | JobResponse:
    await _reject_removed_options(request)
    _validate_source(file, source_url)
    options = _options(
        profile=profile,
        unit_range=unit_range,
        language=language,
        description_language=description_language,
        include_renderings=include_renderings,
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
        response.status_code = status.HTTP_202_ACCEPTED
        return JobResponse.from_record(result)
    if result.parse_result is None:
        raise ServiceError("result_missing", "content parse result was not produced", status_code=500)
    return result.parse_result


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
    profile: Annotated[ParseProfile, Form()] = ParseProfile.BALANCED,
    unit_range: Annotated[
        str | None, Form(description="One-based page or slide ranges, e.g. 1-5,8")
    ] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    description_language: Annotated[Literal["zh-CN", "en", "auto"], Form()] = "zh-CN",
    include_renderings: Annotated[bool, Form()] = True,
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
) -> JobResponse:
    await _reject_removed_options(request)
    _validate_source(file, source_url)
    options = _options(
        profile=profile,
        unit_range=unit_range,
        language=language,
        description_language=description_language,
        include_renderings=include_renderings,
        timeout_seconds=timeout_seconds,
    )
    job = await get_runtime(request).job_service.create_job(
        file=file,
        source_url=source_url,
        options=options,
    )
    return JobResponse.from_record(job)
