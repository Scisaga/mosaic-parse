"""TTL cleanup coordinated across SQLite and local storage."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.repositories.job_repository import JobRepository
from app.services.storage_service import StorageService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    examined: int
    deleted: int
    failed: int


class CleanupService:
    def __init__(self, repository: JobRepository, storage: StorageService) -> None:
        self.repository = repository
        self.storage = storage

    async def cleanup_expired(self, now: datetime | None = None, *, limit: int = 1_000) -> CleanupResult:
        expired = await self.repository.list_expired(now or datetime.now(UTC), limit=limit)
        deleted = failed = 0
        for job in expired:
            if not job.is_terminal:
                # TTL cleanup must never race an active conversion.
                continue
            try:
                # Delete payload first. If metadata deletion fails, a future run can
                # retry safely; the reverse order could orphan large files forever.
                await self.storage.delete_job(job.id)
                await self.repository.delete(job.id)
                deleted += 1
            except (OSError, ValueError):
                failed += 1
                logger.exception("failed to clean expired job", extra={"job_id": job.id})
        return CleanupResult(examined=len(expired), deleted=deleted, failed=failed)

    run = cleanup_expired

    async def cleanup(self, now: datetime | None = None, *, limit: int = 1_000) -> int:
        """Convenience form used by the admin/lifespan layer."""

        return (await self.cleanup_expired(now, limit=limit)).deleted
