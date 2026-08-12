"""Document parsing and job creation endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile, status
from pydantic import ValidationError

from app.api.dependencies import get_runtime, require_api_key
from app.api.schemas import JobResponse, ParseResponse
from app.models import (
    DocumentParseOptions,
    OutputFormat,
    ParseMode,
    ParseProfile,
    ServiceError,
)

router = APIRouter(
    prefix="/v1/documents",
    tags=["documents"],
    dependencies=[Depends(require_api_key)],
)


def _options(
    *,
    mode: ParseMode,
    profile: ParseProfile,
    output_format: OutputFormat,
    page_range: str | None,
    language: str,
    enable_vlm_fallback: bool,
    preserve_page_breaks: bool,
    include_pages: bool,
    include_diagnostics: bool,
    timeout_seconds: int | None,
) -> DocumentParseOptions:
    try:
        return DocumentParseOptions(
            mode=mode,
            profile=profile,
            output_format=output_format,
            page_range=page_range,
            language=[item.strip() for item in language.split(",") if item.strip()],
            enable_vlm_fallback=enable_vlm_fallback,
            preserve_page_breaks=preserve_page_breaks,
            include_pages=include_pages,
            include_diagnostics=include_diagnostics,
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
    summary="Parse a small document synchronously",
    responses={409: {"description": "The source exceeds synchronous limits"}},
)
async def parse_document(
    request: Request,
    response: Response,
    file: Annotated[UploadFile | None, File(description="PDF or supported image")] = None,
    source_url: Annotated[str | None, Form(description="HTTP(S) document URL")] = None,
    mode: Annotated[ParseMode, Form()] = ParseMode.AUTO,
    profile: Annotated[ParseProfile, Form()] = ParseProfile.BALANCED,
    output_format: Annotated[OutputFormat, Form()] = OutputFormat.MARKDOWN,
    page_range: Annotated[str | None, Form(description="One-based ranges, e.g. 1-5,8")] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    enable_vlm_fallback: Annotated[bool, Form()] = False,
    preserve_page_breaks: Annotated[bool, Form()] = True,
    include_pages: Annotated[bool, Form()] = False,
    include_diagnostics: Annotated[bool, Form()] = True,
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
    prefer_async: Annotated[bool, Form()] = False,
) -> ParseResponse | JobResponse:
    _validate_source(file, source_url)
    options = _options(
        mode=mode,
        profile=profile,
        output_format=output_format,
        page_range=page_range,
        language=language,
        enable_vlm_fallback=enable_vlm_fallback,
        preserve_page_breaks=preserve_page_breaks,
        include_pages=include_pages,
        include_diagnostics=include_diagnostics,
        timeout_seconds=timeout_seconds,
    )
    runtime = get_runtime(request)
    if prefer_async:
        job = await runtime.job_service.create_job(
            file=file,
            source_url=source_url,
            options=options,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return JobResponse.from_record(job)

    result = await runtime.job_service.parse_sync(
        file=file,
        source_url=source_url,
        options=options,
    )
    return ParseResponse.from_result(result, options)


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create an asynchronous parsing job",
)
async def create_document_job(
    request: Request,
    file: Annotated[UploadFile | None, File(description="PDF or supported image")] = None,
    source_url: Annotated[str | None, Form(description="HTTP(S) document URL")] = None,
    mode: Annotated[ParseMode, Form()] = ParseMode.AUTO,
    profile: Annotated[ParseProfile, Form()] = ParseProfile.BALANCED,
    output_format: Annotated[OutputFormat, Form()] = OutputFormat.MARKDOWN,
    page_range: Annotated[str | None, Form(description="One-based ranges, e.g. 1-5,8")] = None,
    language: Annotated[str, Form(description="Comma-separated OCR languages")] = "zh,en",
    enable_vlm_fallback: Annotated[bool, Form()] = False,
    preserve_page_breaks: Annotated[bool, Form()] = True,
    include_pages: Annotated[bool, Form()] = False,
    include_diagnostics: Annotated[bool, Form()] = True,
    timeout_seconds: Annotated[int | None, Form(ge=1, le=86_400)] = None,
) -> JobResponse:
    _validate_source(file, source_url)
    options = _options(
        mode=mode,
        profile=profile,
        output_format=output_format,
        page_range=page_range,
        language=language,
        enable_vlm_fallback=enable_vlm_fallback,
        preserve_page_breaks=preserve_page_breaks,
        include_pages=include_pages,
        include_diagnostics=include_diagnostics,
        timeout_seconds=timeout_seconds,
    )
    job = await get_runtime(request).job_service.create_job(
        file=file,
        source_url=source_url,
        options=options,
    )
    return JobResponse.from_record(job)
