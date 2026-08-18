"""In-process asynchronous queue backed by durable SQLite job state."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models.document_ir import ContentEvidenceIR
from app.models.error import ServiceError
from app.models.job import JobEvent, JobProgress, JobRecord, JobStatus
from app.models.parse_options import ContentParseOptions
from app.models.parse_result import DocumentParseResult
from app.models.source import StoredSource
from app.repositories.job_repository import JobRepository
from app.security.file_validation import DOCX_MIME, IMAGE_MIME_TYPES, PPTX_MIME, VIDEO_MIME_TYPES
from app.services.parser_service import ParserService
from app.services.source_service import SourceService
from app.services.storage_service import LegacyEvidenceError, StorageService
from app.utils.ids import new_job_id
from app.utils.page_range import PageRangeError, parse_page_range
from app.utils.settings import setting

logger = logging.getLogger(__name__)


class JobService:
    def __init__(
        self,
        settings: object | None,
        repository: JobRepository,
        storage: StorageService,
        source_service: SourceService,
        parser_service: ParserService,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.source_service = source_service
        self.parser_service = parser_service
        self.max_queued_jobs = int(setting(settings, "max_queued_jobs", 8))
        self.parser_workers = int(setting(settings, "parser_workers", 1))
        self.retention_hours = int(setting(settings, "job_retention_hours", 24))
        self.heartbeat_seconds = float(setting(settings, "sse_heartbeat_seconds", 15.0))
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.max_queued_jobs)
        # This token queue is the atomic admission boundary for asynchronous
        # jobs. A token is acquired before materializing a source and is owned by
        # the queued job after put_nowait succeeds. Workers return it as soon as
        # they dequeue the job, so running work does not consume queue capacity.
        self._async_tokens: asyncio.Queue[None] = asyncio.Queue(maxsize=self.max_queued_jobs)
        for _ in range(self.max_queued_jobs):
            self._async_tokens.put_nowait(None)
        self._sync_tokens: asyncio.Queue[None] = asyncio.Queue(maxsize=self.parser_workers)
        for _ in range(self.parser_workers):
            self._sync_tokens.put_nowait(None)
        self._workers: list[asyncio.Task[None]] = []
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._event_history: dict[str, deque[JobEvent]] = defaultdict(lambda: deque(maxlen=500))
        self._event_conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._event_counters: dict[str, int] = defaultdict(int)
        self._running_jobs: set[str] = set()
        self._live_progress: dict[str, JobProgress] = {}
        self._started = False

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._started:
            return
        await self.storage.initialize()
        await self.repository.initialize()
        await self.parser_service.initialize()
        await self.repository.mark_interrupted_failed()
        # Queued jobs are not recoverable by the in-memory v0.1 queue either.
        for orphan in await self.repository.list(status=JobStatus.QUEUED, limit=10_000):
            await self.repository.fail(
                orphan.id, "server_restarted", "server restarted before the queued job began"
            )
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"parser-worker-{index}")
            for index in range(self.parser_workers)
        ]
        self._started = True

    async def shutdown(self) -> None:
        if self._started:
            for task in self._workers:
                task.cancel()
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()
        # Queued records remain durable and are marked server_restarted on the
        # next start, but their in-memory admission ownership must not leak.
        while True:
            try:
                job_id = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            self._release_async_token()
            self._discard_job_runtime_state(job_id)
        self._started = False

    def _acquire_async_token(self) -> None:
        try:
            self._async_tokens.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise ServiceError("queue_full", "the parsing queue is full", status_code=429) from exc

    def _release_async_token(self) -> None:
        try:
            self._async_tokens.put_nowait(None)
        except asyncio.QueueFull:
            # Do not crash a worker/shutdown path because of an invariant
            # violation; surface it loudly while keeping the queue operational.
            logger.error("asynchronous queue admission token was released twice")

    def _discard_job_runtime_state(self, job_id: str) -> None:
        self._cancel_events.pop(job_id, None)
        self._live_progress.pop(job_id, None)
        self._event_history.pop(job_id, None)
        self._event_conditions.pop(job_id, None)
        self._event_counters.pop(job_id, None)

    async def _next_event_id(self, job_id: str) -> int:
        self._event_counters[job_id] += 1
        return self._event_counters[job_id]

    async def _emit(self, job_id: str, event: str, **data: object) -> JobEvent:
        item = JobEvent(id=await self._next_event_id(job_id), event=event, job_id=job_id, data=data)
        condition = self._event_conditions[job_id]
        async with condition:
            self._event_history[job_id].append(item)
            condition.notify_all()
        return item

    async def _prepare_source(
        self,
        identifier: str,
        *,
        file: object | None,
        source_url: str | None,
        filename: str | None,
        content: bytes | None,
    ) -> StoredSource:
        try:
            return await self.source_service.prepare(
                identifier,
                file=file,
                source_url=source_url,
                filename=filename,
                content=content,
            )
        except BaseException:
            await self.storage.delete_job(identifier)
            raise

    @staticmethod
    def _validate_selection(
        options: ContentParseOptions, page_count: int, mime_type: str | None = None
    ) -> list[int]:
        if mime_type in VIDEO_MIME_TYPES and options.unit_range is not None:
            raise ServiceError(
                "invalid_unit_range", "video inputs do not accept unit_range", status_code=400
            )
        if (
            mime_type == DOCX_MIME or (mime_type in IMAGE_MIME_TYPES and mime_type != "image/tiff")
        ) and options.unit_range not in {None, "1"}:
            raise ServiceError(
                "invalid_unit_range",
                "DOCX and single-frame images only accept an omitted unit_range or 1",
                status_code=400,
            )
        try:
            return parse_page_range(options.page_range, page_count)
        except PageRangeError as exc:
            raise ServiceError("invalid_unit_range", str(exc), status_code=400) from exc

    @staticmethod
    def _progress_unit(mime_type: str) -> str:
        if mime_type == PPTX_MIME:
            return "slide"
        if mime_type in VIDEO_MIME_TYPES:
            return "frame"
        return "page" if mime_type in {"application/pdf", "image/tiff"} else "asset"

    async def _parse_prepared_sync(
        self,
        job_id: str,
        source: StoredSource,
        options: ContentParseOptions,
    ) -> DocumentParseResult:
        selected = self._validate_selection(options, source.page_count, source.mime_type)
        record = JobRecord(
            id=job_id,
            filename=source.filename,
            mime_type=source.mime_type,
            input_bytes=source.size_bytes,
            page_count=source.page_count,
            source_path=str(source.path),
            source_url=source.source_url,
            options=options,
            progress=JobProgress(
                current=0,
                total=len(selected),
                unit=self._progress_unit(source.mime_type),
            ),
            expires_at=datetime.now(UTC) + timedelta(hours=self.retention_hours),
        )
        await self.repository.create(record)
        await self.repository.transition(job_id, JobStatus.RUNNING)
        try:
            result = await self.parser_service.parse(source, options, document_id=job_id)
            paths = await self.storage.write_result(job_id, result)
            partial = bool(result.evidence_ir and result.evidence_ir.status == "partial")
            await self.repository.update_progress(
                job_id, len(selected), len(selected), self._progress_unit(source.mime_type)
            )
            await self.repository.complete(
                job_id,
                ir_path=str(paths.ir),
                markdown_path=str(paths.markdown),
                text_path=str(paths.text),
                partial=partial,
            )
            return result
        except ServiceError as exc:
            current = await self.repository.get(job_id)
            if current is not None and not current.is_terminal:
                await self.repository.fail(job_id, exc.code, exc.message, exc.details)
            raise
        except Exception as exc:
            current = await self.repository.get(job_id)
            if current is not None and not current.is_terminal:
                await self.repository.fail(job_id, "parse_failed", "content parsing failed")
            raise ServiceError("parse_failed", "content parsing failed", status_code=502) from exc

    async def parse_content(
        self,
        *,
        file: object | None = None,
        source_url: str | None = None,
        filename: str | None = None,
        content: bytes | None = None,
        options: ContentParseOptions | dict[str, object] | None = None,
        prefer_async: bool = False,
    ) -> DocumentParseResult | JobRecord:
        """Persist every parse and automatically enqueue videos or large inputs."""

        if not self._started:
            await self.start()
        parsed_options = (
            options
            if isinstance(options, ContentParseOptions)
            else ContentParseOptions.model_validate(options or {})
        )
        job_id = new_job_id()
        source = await self._prepare_source(
            job_id,
            file=file,
            source_url=source_url,
            filename=filename,
            content=content,
        )
        try:
            selected = self._validate_selection(parsed_options, source.page_count, source.mime_type)
        except BaseException:
            await self.storage.delete_job(job_id)
            raise
        use_async = (
            prefer_async
            or source.mime_type in VIDEO_MIME_TYPES
            or source.size_bytes > int(setting(self.settings, "sync_max_bytes", 20 * 1024 * 1024))
            or len(selected) > int(setting(self.settings, "sync_max_units", 10))
        )
        if use_async:
            self._acquire_async_token()
            admission_owned = True
            try:
                record = await self._enqueue_source(job_id, source, parsed_options)
                admission_owned = False
                return record
            finally:
                if admission_owned:
                    await self.storage.delete_job(job_id)
                    self._release_async_token()
        try:
            self._sync_tokens.get_nowait()
        except asyncio.QueueEmpty as exc:
            await self.storage.delete_job(job_id)
            raise ServiceError(
                "sync_capacity_exceeded",
                "all synchronous parser slots are busy; retry with prefer_async=true",
                status_code=429,
                details={"capacity": self.parser_workers},
            ) from exc
        try:
            return await self._parse_prepared_sync(job_id, source, parsed_options)
        finally:
            self._sync_tokens.put_nowait(None)

    async def parse_sync(
        self,
        *,
        file: object | None = None,
        source_url: str | None = None,
        filename: str | None = None,
        content: bytes | None = None,
        options: ContentParseOptions | dict[str, object] | None = None,
    ) -> DocumentParseResult:
        parsed_options = (
            options
            if isinstance(options, ContentParseOptions)
            else ContentParseOptions.model_validate(options or {})
        )
        try:
            self._sync_tokens.get_nowait()
        except asyncio.QueueEmpty as exc:
            raise ServiceError(
                "sync_capacity_exceeded",
                "all synchronous parser slots are busy; retry or use the asynchronous jobs endpoint",
                status_code=429,
                details={"capacity": self.parser_workers},
            ) from exc
        identifier = new_job_id()
        durable_record_created = False
        try:
            source = await self._prepare_source(
                identifier,
                file=file,
                source_url=source_url,
                filename=filename,
                content=content,
            )
            pages = self._validate_selection(parsed_options, source.page_count, source.mime_type)
            sync_max_bytes = int(setting(self.settings, "sync_max_bytes", 20 * 1024 * 1024))
            sync_max_units = int(setting(self.settings, "sync_max_units", 10))
            if source.size_bytes > sync_max_bytes or len(pages) > sync_max_units:
                raise ServiceError(
                    "sync_limit_exceeded",
                    "content exceeds synchronous limits; use parse_content or the jobs endpoint",
                    status_code=409,
                    details={
                        "input_bytes": source.size_bytes,
                        "selected_units": len(pages),
                        "sync_max_bytes": sync_max_bytes,
                        "sync_max_units": sync_max_units,
                    },
                )
            result = await self._parse_prepared_sync(identifier, source, parsed_options)
            durable_record_created = True
            return result
        finally:
            if not durable_record_created:
                record = await self.repository.get(identifier)
                if record is None:
                    await self.storage.delete_job(identifier)
            self._sync_tokens.put_nowait(None)

    async def _enqueue_source(
        self,
        job_id: str,
        source: StoredSource,
        options: ContentParseOptions,
        *,
        attempt: int = 1,
        parent_job_id: str | None = None,
    ) -> JobRecord:
        pages = self._validate_selection(options, source.page_count, source.mime_type)
        record = JobRecord(
            id=job_id,
            filename=source.filename,
            mime_type=source.mime_type,
            input_bytes=source.size_bytes,
            page_count=source.page_count,
            source_path=str(source.path),
            source_url=source.source_url,
            options=options,
            progress=JobProgress(
                current=0, total=len(pages), unit=self._progress_unit(source.mime_type)
            ),
            expires_at=datetime.now(UTC) + timedelta(hours=self.retention_hours),
            attempt=attempt,
            parent_job_id=parent_job_id,
        )
        try:
            await self.repository.create(record)
            self._cancel_events[job_id] = asyncio.Event()
            await self._emit(job_id, "job.queued", current=0, total=len(pages))
            # This is deliberately the final await-free step. Once it succeeds,
            # admission ownership has transferred from the caller to this queue
            # item and cancellation cannot strand an ambiguous owner.
            self._queue.put_nowait(job_id)
        except BaseException as exc:
            try:
                await self.repository.delete(job_id)
            finally:
                self._discard_job_runtime_state(job_id)
            if isinstance(exc, asyncio.QueueFull):
                raise ServiceError(
                    "queue_full", "the parsing queue is full", status_code=429
                ) from exc
            raise
        return record

    async def create_job(
        self,
        *,
        file: object | None = None,
        source_url: str | None = None,
        filename: str | None = None,
        content: bytes | None = None,
        options: ContentParseOptions | dict[str, object] | None = None,
    ) -> JobRecord:
        if not self._started:
            await self.start()
        parsed_options = (
            options
            if isinstance(options, ContentParseOptions)
            else ContentParseOptions.model_validate(options or {})
        )
        job_id = new_job_id()
        self._acquire_async_token()
        admission_owned = True
        try:
            source = await self._prepare_source(
                job_id,
                file=file,
                source_url=source_url,
                filename=filename,
                content=content,
            )
            record = await self._enqueue_source(job_id, source, parsed_options)
            admission_owned = False
            return record
        finally:
            if admission_owned:
                try:
                    await self.storage.delete_job(job_id)
                finally:
                    self._release_async_token()

    async def get_job(self, job_id: str) -> JobRecord:
        record = await self.repository.get(job_id)
        if record is None:
            raise ServiceError("job_not_found", "job does not exist", status_code=404)
        live_progress = self._live_progress.get(job_id)
        if live_progress is not None and not record.is_terminal:
            record.progress = live_progress.model_copy()
        return record

    async def _worker(self, worker_index: int) -> None:
        while True:
            job_id = await self._queue.get()
            # Admission measures queued work only; return capacity before any
            # repository or parser await so worker cancellation cannot leak it.
            self._release_async_token()
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unhandled job worker failure", extra={"job_id": job_id})
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        record = await self.repository.get(job_id)
        if record is None or record.status == JobStatus.CANCELLED:
            return
        self._running_jobs.add(job_id)
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
        try:
            record = await self.repository.transition(job_id, JobStatus.RUNNING)
            await self._emit(job_id, "job.started", worker="internal")
            source = StoredSource(
                path=Path(record.source_path),
                filename=record.filename,
                mime_type=record.mime_type,
                size_bytes=record.input_bytes,
                page_count=record.page_count,
                source_url=record.source_url,
            )
            selected_pages = self._validate_selection(
                record.options, record.page_count, record.mime_type
            )
            live_progress_current = record.progress.current
            final_progress_total = record.progress.total
            self._live_progress[job_id] = record.progress.model_copy()

            async def progress(current: int, total: int, state: str) -> None:
                nonlocal final_progress_total, live_progress_current
                if cancel_event.is_set():
                    return
                terminal_page = state in {"page.completed", "page.warning", "page.failed"}
                unit_processed = state in {"unit.processed", "frame.processed"}
                postprocess = state.startswith("postprocess.")
                # ``page.processed`` is emitted from Docling's real internal
                # page boundary before final document assembly/export. It is
                # useful live progress, but only final page events advance the
                # durable completed-page counter.
                if terminal_page or unit_processed or state == "document.started":
                    await self.repository.update_progress(
                        job_id, current, total, self._progress_unit(record.mime_type)
                    )
                    final_progress_total = total
                event_name = (
                    state
                    if state in {"page.completed", "page.warning", "page.failed"}
                    else "job.progress"
                )
                if postprocess:
                    # Post-processing starts only after every selected page has
                    # completed Docling export. Keep the phase visible without
                    # regressing the live page counter or overwriting durable
                    # progress with a secondary adapter's local range.
                    live_progress_current = max(live_progress_current, total)
                elif state == "page.processed":
                    live_progress_current = max(live_progress_current, current)
                elif terminal_page or unit_processed:
                    live_progress_current = max(live_progress_current, current)
                elif state == "document.started":
                    live_progress_current = current
                display_total = record.progress.total if postprocess else total
                self._live_progress[job_id] = JobProgress(
                    current=live_progress_current,
                    total=display_total,
                    unit=self._progress_unit(record.mime_type),
                )
                event_data: dict[str, object] = {
                    "current": live_progress_current,
                    "total": display_total,
                    "percent": (
                        round(live_progress_current * 100 / display_total, 1)
                        if display_total
                        else 0.0
                    ),
                    "phase": "page_pipeline" if state == "page.processed" else state,
                }
                if terminal_page and 1 <= current <= len(selected_pages):
                    event_data["page_number"] = selected_pages[current - 1]
                await self._emit(job_id, event_name, **event_data)

            result = await self.parser_service.parse(
                source,
                record.options,
                document_id=job_id,
                progress_callback=progress,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                current = await self.repository.get(job_id)
                if current and current.status != JobStatus.CANCELLED:
                    await self.repository.cancel(job_id)
                await self._emit(job_id, "job.cancelled")
                return
            paths = await self.storage.write_result(job_id, result)
            await self.repository.update_progress(
                job_id,
                final_progress_total,
                final_progress_total,
                self._progress_unit(record.mime_type),
            )
            partial = bool(result.evidence_ir and result.evidence_ir.status == "partial")
            await self.repository.complete(
                job_id,
                ir_path=str(paths.ir),
                markdown_path=str(paths.markdown),
                text_path=str(paths.text),
                partial=partial,
            )
            await self._emit(
                job_id,
                "job.completed",
                status="partial" if partial else "completed",
                failed_units=sum(
                    unit.status == "failed"
                    for unit in (result.evidence_ir.units if result.evidence_ir else [])
                ),
            )
        except asyncio.CancelledError:
            # Preserve running state so the next process marks this as
            # failed/server_restarted, matching the persisted-state contract.
            raise
        except ServiceError as exc:
            current = await self.repository.get(job_id)
            if current and current.status == JobStatus.CANCELLED:
                await self._emit(job_id, "job.cancelled")
            elif current and not current.is_terminal:
                await self.repository.fail(job_id, exc.code, exc.message, exc.details)
                await self._emit(job_id, "job.failed", code=exc.code, message=exc.message)
        except Exception as exc:
            current = await self.repository.get(job_id)
            if current and not current.is_terminal:
                await self.repository.fail(job_id, "parse_failed", "content parsing failed")
                await self._emit(
                    job_id, "job.failed", code="parse_failed", message=type(exc).__name__
                )
            logger.exception("job parsing failed", extra={"job_id": job_id})
        finally:
            self._running_jobs.discard(job_id)
            self._cancel_events.pop(job_id, None)
            self._live_progress.pop(job_id, None)

    async def retry_job(self, job_id: str, *, unit_range: str | None = None) -> JobRecord:
        if not self._started:
            await self.start()
        original = await self.get_job(job_id)
        if original.status not in {JobStatus.FAILED, JobStatus.PARTIAL, JobStatus.CANCELLED}:
            raise ServiceError(
                "job_not_retryable",
                "only failed, partial, or cancelled jobs can be retried",
                status_code=409,
            )
        options = (
            original.options.model_copy(update={"unit_range": unit_range})
            if unit_range is not None
            else original.options.model_copy()
        )
        new_id = new_job_id()
        original_source = StoredSource(
            path=Path(original.source_path),
            filename=original.filename,
            mime_type=original.mime_type,
            size_bytes=original.input_bytes,
            page_count=original.page_count,
            source_url=original.source_url,
        )
        if not original_source.path.is_file():
            raise ServiceError(
                "source_expired", "the original source file is no longer available", status_code=409
            )
        self._acquire_async_token()
        admission_owned = True
        try:
            copied = await self.storage.copy_source(original_source, new_id)
            record = await self._enqueue_source(
                new_id,
                copied,
                options,
                attempt=original.attempt + 1,
                parent_job_id=original.id,
            )
            admission_owned = False
            return record
        finally:
            if admission_owned:
                try:
                    await self.storage.delete_job(new_id)
                finally:
                    self._release_async_token()

    async def cancel_job(self, job_id: str) -> JobRecord:
        record = await self.get_job(job_id)
        if record.is_terminal:
            if record.status == JobStatus.CANCELLED:
                return record
            raise ServiceError(
                "job_not_cancellable", "completed jobs cannot be cancelled", status_code=409
            )
        self._cancel_events.setdefault(job_id, asyncio.Event()).set()
        try:
            cancelled = await self.repository.cancel(job_id)
        except ValueError as exc:
            latest = await self.get_job(job_id)
            if latest.status == JobStatus.CANCELLED:
                return latest
            raise ServiceError(
                "job_not_cancellable", "job finished before cancellation", status_code=409
            ) from exc
        await self._emit(job_id, "job.cancelled")
        return cancelled

    async def delete_job(self, job_id: str) -> bool:
        record = await self.get_job(job_id)
        if not record.is_terminal:
            raise ServiceError(
                "job_active", "cancel an active job before deleting it", status_code=409
            )
        await self.storage.delete_job(job_id)
        deleted = await self.repository.delete(job_id)
        self._event_history.pop(job_id, None)
        self._event_conditions.pop(job_id, None)
        self._event_counters.pop(job_id, None)
        return deleted

    async def get_result(self, job_id: str, representation: str = "ir") -> str:
        record = await self.get_job(job_id)
        if record.status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
            raise ServiceError("result_not_ready", "job result is not available", status_code=409)
        try:
            return await self.storage.read_result(job_id, representation)
        except FileNotFoundError as exc:
            raise ServiceError(
                "result_missing", "persisted result file is missing", status_code=500
            ) from exc

    async def get_evidence(self, job_id: str) -> ContentEvidenceIR:
        record = await self.get_job(job_id)
        if record.status not in {JobStatus.COMPLETED, JobStatus.PARTIAL}:
            raise ServiceError("result_not_ready", "job result is not available", status_code=409)
        try:
            return await self.storage.read_evidence(job_id)
        except LegacyEvidenceError as exc:
            raise ServiceError(
                "legacy_result_contract",
                "this retained job uses the retired evidence contract and is not converted",
                status_code=409,
            ) from exc
        except FileNotFoundError as exc:
            raise ServiceError(
                "result_missing", "persisted evidence IR is missing", status_code=500
            ) from exc

    async def events(
        self, job_id: str, last_event_id: int | None = None
    ) -> AsyncIterator[JobEvent]:
        await self.get_job(job_id)
        seen = max(0, last_event_id or 0)
        condition = self._event_conditions[job_id]
        while True:
            pending = [event for event in self._event_history[job_id] if event.id > seen]
            if pending:
                for event in pending:
                    seen = event.id
                    yield event
                continue
            record = await self.get_job(job_id)
            if record.is_terminal:
                return
            try:
                async with condition:
                    # Close the check/wait race: emitters hold the same condition
                    # while appending and notifying.
                    if any(event.id > seen for event in self._event_history[job_id]):
                        continue
                    await asyncio.wait_for(condition.wait(), timeout=self.heartbeat_seconds)
            except TimeoutError:
                heartbeat = JobEvent(
                    id=await self._next_event_id(job_id),
                    event="heartbeat",
                    job_id=job_id,
                    data={"status": record.status.value},
                )
                seen = heartbeat.id
                yield heartbeat
