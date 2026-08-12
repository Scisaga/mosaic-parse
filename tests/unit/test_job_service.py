from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.models import (
    DocumentParseOptions,
    DocumentParseResult,
    JobRecord,
    JobStatus,
    PageParseResult,
    ParsePipeline,
    RouteSummary,
    ServiceError,
)
from app.repositories import JobRepository
from app.services.job_service import JobService
from app.services.source_service import SourceService
from app.services.storage_service import StorageService


class BlockingParserService:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def initialize(self) -> None:
        return None

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        self.entered.set()
        await self.release.wait()
        return DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=1,
            pages=[PageParseResult(page_number=1, backend="fake", content="# Page 1")],
            pipeline=ParsePipeline(mode="auto", profile="balanced", primary="fake"),
            route_summary=RouteSummary(failed_pages=0),
        )


class PipelineProgressParserService(BlockingParserService):
    def __init__(self) -> None:
        super().__init__()
        self.progress_emitted = asyncio.Event()

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        assert progress_callback is not None
        await progress_callback(0, source.page_count, "document.started")
        await progress_callback(1, source.page_count, "page.processed")
        self.progress_emitted.set()
        await self.release.wait()
        return DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=1,
            pages=[PageParseResult(page_number=1, backend="fake", content="# Page 1")],
            pipeline=ParsePipeline(mode="auto", profile="balanced", primary="fake"),
            route_summary=RouteSummary(failed_pages=0),
        )


class PostprocessProgressParserService(BlockingParserService):
    def __init__(self) -> None:
        super().__init__()
        self.postprocess_emitted = asyncio.Event()

    async def parse(self, source, options, *, document_id, progress_callback=None, cancel_event=None):
        assert progress_callback is not None
        await progress_callback(source.page_count, source.page_count, "page.completed")
        # Secondary adapters often operate on a one-page sub-range. The job
        # service must retain the whole-document total/counter for this phase.
        await progress_callback(1, 1, "postprocess.diagram")
        self.postprocess_emitted.set()
        await self.release.wait()
        return DocumentParseResult(
            document_id=document_id,
            filename=source.filename,
            mime_type=source.mime_type,
            page_count=source.page_count,
            processed_pages=source.page_count,
            pages=[PageParseResult(page_number=1, backend="fake", content="# Page 1")],
            pipeline=ParsePipeline(mode="auto", profile="balanced", primary="fake"),
            route_summary=RouteSummary(failed_pages=0),
        )


class BarrierSourceService:
    def __init__(self, delegate: SourceService) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.prepare_calls = 0

    async def prepare(self, identifier: str, **kwargs):
        self.prepare_calls += 1
        self.entered.set()
        await self.release.wait()
        return await self.delegate.prepare(identifier, **kwargs)


def async_job_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        parser_workers=1,
        max_queued_jobs=1,
        glm_ocr_enabled=False,
        vlm_enabled=False,
        max_upload_bytes=1_000_000,
        sync_max_bytes=1_000_000,
        sync_max_pages=10,
    )


async def test_sync_admission_is_immediate_bounded_and_cleans_sources(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        parser_workers=1,
        glm_ocr_enabled=False,
        vlm_enabled=False,
        max_upload_bytes=1_000_000,
        sync_max_bytes=1_000_000,
        sync_max_pages=10,
    )
    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = BlockingParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    content = Path("tests/fixtures/native-report.pdf").read_bytes()

    first = asyncio.create_task(
        jobs.parse_sync(filename="first.pdf", content=content, options=DocumentParseOptions())
    )
    await parser.entered.wait()
    with pytest.raises(ServiceError) as caught:
        await jobs.parse_sync(filename="second.pdf", content=content, options=DocumentParseOptions())
    assert caught.value.code == "sync_capacity_exceeded"
    assert caught.value.status_code == 429
    assert caught.value.details == {"capacity": 1}

    parser.release.set()
    await first
    assert list(storage.jobs_dir.iterdir()) == []

    # The token is returned even through the cleanup path.
    await jobs.parse_sync(filename="third.pdf", content=content, options=DocumentParseOptions())
    assert list(storage.jobs_dir.iterdir()) == []
    await source.close()


async def test_live_pipeline_progress_is_visible_without_marking_a_page_durable(
    tmp_path: Path,
) -> None:
    settings = async_job_settings(tmp_path)
    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = PipelineProgressParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    await jobs.start()

    record = await jobs.create_job(
        filename="progress.pdf",
        content=Path("tests/fixtures/native-report.pdf").read_bytes(),
    )
    await parser.progress_emitted.wait()

    live = await jobs.get_job(record.id)
    durable = await repository.require(record.id)
    assert live.progress.current == 1
    assert durable.progress.current == 0
    history = list(jobs._event_history[record.id])
    progress_event = next(item for item in history if item.data.get("phase") == "page_pipeline")
    assert progress_event.event == "job.progress"
    assert progress_event.data["current"] == 1

    parser.release.set()
    while not (await repository.require(record.id)).is_terminal:
        await asyncio.sleep(0)
    await jobs.shutdown()
    await source.close()


async def test_postprocess_phase_does_not_regress_whole_document_progress(tmp_path: Path) -> None:
    settings = async_job_settings(tmp_path)
    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = PostprocessProgressParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    await jobs.start()

    record = await jobs.create_job(
        filename="progress.pdf",
        content=Path("tests/fixtures/native-report.pdf").read_bytes(),
    )
    await parser.postprocess_emitted.wait()

    live = await jobs.get_job(record.id)
    assert live.progress.current == live.progress.total == record.page_count
    event = next(
        item
        for item in jobs._event_history[record.id]
        if item.data.get("phase") == "postprocess.diagram"
    )
    assert event.data["current"] == event.data["total"] == record.page_count

    parser.release.set()
    while not (await repository.require(record.id)).is_terminal:
        await asyncio.sleep(0)
    await jobs.shutdown()
    await source.close()


async def test_async_create_admission_precedes_prepare_and_reuses_after_dequeue(tmp_path: Path) -> None:
    settings = async_job_settings(tmp_path)
    storage = StorageService(settings)
    repository = JobRepository(settings)
    real_source = SourceService(settings, storage)
    source = BarrierSourceService(real_source)
    parser = BlockingParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    content = Path("tests/fixtures/native-report.pdf").read_bytes()
    await jobs.start()

    first = asyncio.create_task(jobs.create_job(filename="first.pdf", content=content))
    await source.entered.wait()
    before = set(storage.jobs_dir.iterdir())
    with pytest.raises(ServiceError) as caught:
        await jobs.create_job(filename="excess.pdf", content=content)
    assert caught.value.code == "queue_full"
    assert caught.value.status_code == 429
    assert source.prepare_calls == 1
    assert set(storage.jobs_dir.iterdir()) == before

    source.release.set()
    first_record = await first
    await parser.entered.wait()
    assert (await repository.require(first_record.id)).status == JobStatus.RUNNING
    assert jobs._async_tokens.qsize() == 1

    # A running parse does not consume queued capacity: the worker returned the
    # first token at dequeue, so another source can now be admitted.
    second_record = await jobs.create_job(filename="second.pdf", content=content)
    assert second_record.status == JobStatus.QUEUED
    assert source.prepare_calls == 2
    assert jobs.queue_depth == 1
    assert jobs._async_tokens.qsize() == 0

    await jobs.shutdown()
    assert jobs.queue_depth == 0
    assert jobs._async_tokens.qsize() == 1
    await real_source.close()


async def test_retry_admission_precedes_copy_and_token_is_reusable(tmp_path: Path, monkeypatch) -> None:
    settings = async_job_settings(tmp_path)
    storage = StorageService(settings)
    repository = JobRepository(settings)
    real_source = SourceService(settings, storage)
    source = BarrierSourceService(real_source)
    parser = BlockingParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    content_path = Path("tests/fixtures/native-report.pdf").resolve()
    content = content_path.read_bytes()
    await jobs.start()
    await repository.create(
        JobRecord(
            id="job_retry_original",
            status=JobStatus.FAILED,
            filename=content_path.name,
            mime_type="application/pdf",
            input_bytes=content_path.stat().st_size,
            page_count=1,
            source_path=str(content_path),
        )
    )
    copy_calls = 0
    copy_should_fail = False
    copy_source = storage.copy_source

    async def counted_copy(*args, **kwargs):
        nonlocal copy_calls, copy_should_fail
        copy_calls += 1
        if copy_should_fail:
            raise RuntimeError("injected copy failure")
        return await copy_source(*args, **kwargs)

    monkeypatch.setattr(storage, "copy_source", counted_copy)
    occupying = asyncio.create_task(jobs.create_job(filename="occupying.pdf", content=content))
    await source.entered.wait()
    before = set(storage.jobs_dir.iterdir())

    with pytest.raises(ServiceError) as caught:
        await jobs.retry_job("job_retry_original")
    assert caught.value.code == "queue_full"
    assert caught.value.status_code == 429
    assert copy_calls == 0
    assert set(storage.jobs_dir.iterdir()) == before

    source.release.set()
    await occupying
    await parser.entered.wait()
    copy_should_fail = True
    existing_layouts = set(storage.jobs_dir.iterdir())
    with pytest.raises(RuntimeError, match="injected copy failure"):
        await jobs.retry_job("job_retry_original")
    assert jobs._async_tokens.qsize() == 1
    assert set(storage.jobs_dir.iterdir()) == existing_layouts

    copy_should_fail = False
    retried = await jobs.retry_job("job_retry_original")
    assert retried.parent_job_id == "job_retry_original"
    assert retried.attempt == 2
    assert copy_calls == 2

    await jobs.shutdown()
    assert jobs._async_tokens.qsize() == 1
    await real_source.close()


async def test_async_admission_recovers_from_prepare_repository_and_put_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = async_job_settings(tmp_path)
    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = BlockingParserService()
    jobs = JobService(settings, repository, storage, source, parser)  # type: ignore[arg-type]
    content = Path("tests/fixtures/native-report.pdf").read_bytes()
    await jobs.start()

    prepare_source = source.prepare

    async def fail_prepare(*args, **kwargs):
        raise RuntimeError("injected prepare failure")

    monkeypatch.setattr(source, "prepare", fail_prepare)
    with pytest.raises(RuntimeError, match="injected prepare failure"):
        await jobs.create_job(filename="prepare.pdf", content=content)
    assert jobs._async_tokens.qsize() == 1
    assert list(storage.jobs_dir.iterdir()) == []
    monkeypatch.setattr(source, "prepare", prepare_source)

    create_record = repository.create

    async def fail_create(*args, **kwargs):
        raise RuntimeError("injected repository failure")

    monkeypatch.setattr(repository, "create", fail_create)
    with pytest.raises(RuntimeError, match="injected repository failure"):
        await jobs.create_job(filename="repository.pdf", content=content)
    assert jobs._async_tokens.qsize() == 1
    assert list(storage.jobs_dir.iterdir()) == []
    monkeypatch.setattr(repository, "create", create_record)

    put_job = jobs._queue.put_nowait

    def fail_put(job_id: str) -> None:
        raise asyncio.QueueFull

    monkeypatch.setattr(jobs._queue, "put_nowait", fail_put)
    with pytest.raises(ServiceError) as caught:
        await jobs.create_job(filename="put.pdf", content=content)
    assert caught.value.code == "queue_full"
    assert caught.value.status_code == 429
    assert jobs._async_tokens.qsize() == 1
    assert list(storage.jobs_dir.iterdir()) == []
    assert await repository.list() == []
    assert jobs._cancel_events == {}
    monkeypatch.setattr(jobs._queue, "put_nowait", put_job)

    await jobs.shutdown()
    await source.close()
