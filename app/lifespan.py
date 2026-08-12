"""Application-owned service graph and startup/shutdown lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from app.config import Settings

if TYPE_CHECKING:
    from mcp.server import MCPServer

    from app.repositories import JobRepository
    from app.services.cleanup_service import CleanupService
    from app.services.job_service import JobService
    from app.services.parser_service import ParserService
    from app.services.source_service import SourceService
    from app.services.storage_service import StorageService


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    settings: Settings
    repository: JobRepository
    storage_service: StorageService
    source_service: SourceService
    parser_service: ParserService
    job_service: JobService
    cleanup_service: CleanupService
    started_monotonic: float = field(default_factory=time.monotonic)
    _cleanup_task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_monotonic)

    async def start(self) -> None:
        await self.storage_service.initialize()
        await self.repository.initialize()
        interrupted = await self.repository.mark_interrupted_failed()
        if interrupted:
            logger.warning("marked interrupted jobs failed", extra={"status": interrupted})
        await self.parser_service.initialize()
        await self.job_service.start()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="job-retention-cleanup")

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        await self.job_service.shutdown()
        await self.parser_service.close()
        await self.repository.close()

    async def cleanup_expired(self) -> int:
        result = await self.cleanup_service.cleanup_expired()
        return result.deleted

    async def _cleanup_loop(self) -> None:
        interval = max(60.0, min(3600.0, self.settings.job_retention_hours * 1800.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.cleanup_expired()
            except Exception:
                logger.exception("periodic job cleanup failed")

    async def ready_checks(self) -> tuple[dict[str, bool], list[Any]]:
        writable_data, writable_database, backends = await asyncio.gather(
            self.storage_service.is_writable(),
            self.repository.ping(),
            self.parser_service.probe_backends(),
        )
        docling = any(item.name == "docling-standard" and item.ready for item in backends)
        return (
            {
                "writable_data_dir": writable_data,
                "writable_database": writable_database,
                "docling": docling,
            },
            backends,
        )


def build_runtime(settings: Settings) -> Runtime:
    """Construct services without starting model or worker resources."""

    from app.repositories import JobRepository
    from app.services.cleanup_service import CleanupService
    from app.services.job_service import JobService
    from app.services.parser_service import ParserService
    from app.services.source_service import SourceService
    from app.services.storage_service import StorageService

    storage = StorageService(settings)
    repository = JobRepository(settings)
    source = SourceService(settings, storage)
    parser = ParserService(settings)
    jobs = JobService(settings, repository, storage, source, parser)
    cleanup = CleanupService(repository, storage)
    return Runtime(
        settings=settings,
        repository=repository,
        storage_service=storage,
        source_service=source,
        parser_service=parser,
        job_service=jobs,
        cleanup_service=cleanup,
    )


type RuntimeFactory = Callable[[Settings], Runtime | Awaitable[Runtime] | Any]


def create_lifespan(
    settings: Settings,
    *,
    runtime_ref: MutableMapping[str, Any],
    mcp_server: MCPServer[Any] | None = None,
    runtime_factory: RuntimeFactory | None = None,
):
    """Return a FastAPI lifespan sharing one Runtime with HTTP and MCP."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        factory = runtime_factory or build_runtime
        created = factory(settings)
        runtime = await created if inspect.isawaitable(created) else created
        runtime_ref["runtime"] = runtime
        app.state.runtime = runtime
        started = False
        try:
            await runtime.start()
            started = True
            async with AsyncExitStack() as stack:
                if mcp_server is not None:
                    await stack.enter_async_context(mcp_server.session_manager.run())
                yield
        finally:
            if started:
                await runtime.close()
            runtime_ref.pop("runtime", None)
            app.state.runtime = None

    return lifespan
