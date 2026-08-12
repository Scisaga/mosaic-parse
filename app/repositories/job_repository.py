"""SQLite-backed job metadata repository.

SQLite is the durable source of truth.  A complete JSON record is stored for
forward-compatible model evolution while frequently queried fields are indexed in
normal columns.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models.job import TERMINAL_JOB_STATUSES, JobError, JobProgress, JobRecord, JobStatus


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobRepository:
    def __init__(
        self,
        db_path_or_settings: str | Path | object | None = None,
        *,
        db_path: str | Path | None = None,
        settings: object | None = None,
    ) -> None:
        source = db_path if db_path is not None else (settings if settings is not None else db_path_or_settings)
        if isinstance(source, (str, Path)):
            candidate = Path(source)
            self.db_path = candidate if candidate.suffix == ".db" else candidate / "jobs.db"
        else:
            configured = getattr(source, "jobs_db_path", None) or getattr(source, "database_path", None)
            if configured:
                self.db_path = Path(configured)
            else:
                self.db_path = Path(getattr(source, "data_dir", "data")) / "jobs.db"
        self._lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def initialize_sync() -> None:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        input_bytes INTEGER NOT NULL,
                        page_count INTEGER NOT NULL,
                        progress_current INTEGER NOT NULL,
                        progress_total INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        expires_at TEXT,
                        error_code TEXT,
                        parent_job_id TEXT,
                        record_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                    CREATE INDEX IF NOT EXISTS idx_jobs_expires_at ON jobs(expires_at);
                    CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
                    """
                )

        async with self._lock:
            await asyncio.to_thread(initialize_sync)
            self._initialized = True

    async def close(self) -> None:
        # Connections are intentionally short-lived so repository calls are safe
        # when executed from different asyncio worker tasks.
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self.initialize()

    @staticmethod
    def _values(job: JobRecord) -> tuple[Any, ...]:
        return (
            job.id,
            job.status.value,
            job.filename,
            job.mime_type,
            job.input_bytes,
            job.page_count,
            job.progress.current,
            job.progress.total,
            job.created_at.isoformat(),
            job.updated_at.isoformat(),
            job.started_at.isoformat() if job.started_at else None,
            job.completed_at.isoformat() if job.completed_at else None,
            job.expires_at.isoformat() if job.expires_at else None,
            job.error.code if job.error else None,
            job.parent_job_id,
            job.model_dump_json(),
        )

    @staticmethod
    def _insert_sql(*, replace: bool) -> str:
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        return f"""
            {verb} INTO jobs (
                id,status,filename,mime_type,input_bytes,page_count,
                progress_current,progress_total,created_at,updated_at,
                started_at,completed_at,expires_at,error_code,parent_job_id,record_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """

    async def create(self, job: JobRecord) -> JobRecord:
        await self._ensure_initialized()
        job.updated_at = _utc_now()

        def create_sync() -> None:
            with self._connect() as connection:
                connection.execute(self._insert_sql(replace=False), self._values(job))

        try:
            async with self._lock:
                await asyncio.to_thread(create_sync)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"job already exists: {job.id}") from exc
        return job

    create_job = create

    def _get_sync(self, job_id: str, connection: sqlite3.Connection | None = None) -> JobRecord | None:
        owns_connection = connection is None
        current = connection or self._connect()
        try:
            row = current.execute("SELECT record_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            return JobRecord.model_validate_json(row["record_json"]) if row else None
        finally:
            if owns_connection:
                current.close()

    async def get(self, job_id: str) -> JobRecord | None:
        await self._ensure_initialized()
        async with self._lock:
            return await asyncio.to_thread(self._get_sync, job_id)

    get_job = get

    async def require(self, job_id: str) -> JobRecord:
        job = await self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    async def update(self, job: JobRecord) -> JobRecord:
        await self._ensure_initialized()
        job.updated_at = _utc_now()

        def update_sync() -> None:
            with self._connect() as connection:
                if connection.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone() is None:
                    raise KeyError(job.id)
                connection.execute(self._insert_sql(replace=True), self._values(job))

        async with self._lock:
            await asyncio.to_thread(update_sync)
        return job

    update_job = update

    async def transition(
        self,
        job_id: str,
        to_status: JobStatus | str,
        *,
        error: JobError | None = None,
        result_markdown_path: str | None = None,
        result_text_path: str | None = None,
        metadata_path: str | None = None,
    ) -> JobRecord:
        await self._ensure_initialized()
        target = JobStatus(to_status)

        def transition_sync() -> JobRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = self._get_sync(job_id, connection)
                if job is None:
                    raise KeyError(job_id)
                job.ensure_transition(target)
                now = _utc_now()
                job.status = target
                job.updated_at = now
                if target == JobStatus.RUNNING and job.started_at is None:
                    job.started_at = now
                if target in TERMINAL_JOB_STATUSES:
                    job.completed_at = now
                if error is not None:
                    job.error = error
                if result_markdown_path is not None:
                    job.result_markdown_path = result_markdown_path
                if result_text_path is not None:
                    job.result_text_path = result_text_path
                if metadata_path is not None:
                    job.metadata_path = metadata_path
                connection.execute(self._insert_sql(replace=True), self._values(job))
                return job

        async with self._lock:
            return await asyncio.to_thread(transition_sync)

    async def update_progress(self, job_id: str, current: int, total: int, unit: str = "page") -> JobRecord:
        await self._ensure_initialized()
        if current < 0 or total < 0 or (total and current > total):
            raise ValueError("invalid job progress")

        def progress_sync() -> JobRecord:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                job = self._get_sync(job_id, connection)
                if job is None:
                    raise KeyError(job_id)
                if job.is_terminal:
                    return job
                job.progress = JobProgress(current=current, total=total, unit=unit)
                job.updated_at = _utc_now()
                connection.execute(self._insert_sql(replace=True), self._values(job))
                return job

        async with self._lock:
            return await asyncio.to_thread(progress_sync)

    async def complete(
        self,
        job_id: str,
        *,
        markdown_path: str,
        text_path: str,
        metadata_path: str,
        partial: bool = False,
    ) -> JobRecord:
        return await self.transition(
            job_id,
            JobStatus.PARTIAL if partial else JobStatus.COMPLETED,
            result_markdown_path=markdown_path,
            result_text_path=text_path,
            metadata_path=metadata_path,
        )

    async def fail(
        self,
        job_id: str,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> JobRecord:
        return await self.transition(job_id, JobStatus.FAILED, error=JobError(code=code, message=message, details=details))

    async def cancel(self, job_id: str) -> JobRecord:
        return await self.transition(job_id, JobStatus.CANCELLED)

    async def delete(self, job_id: str) -> bool:
        await self._ensure_initialized()

        def delete_sync() -> bool:
            with self._connect() as connection:
                cursor = connection.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                return cursor.rowcount > 0

        async with self._lock:
            return await asyncio.to_thread(delete_sync)

    delete_job = delete

    async def list_expired(self, now: datetime | None = None, *, limit: int = 1_000) -> list[JobRecord]:
        await self._ensure_initialized()
        cutoff = (now or _utc_now()).isoformat()

        def list_sync() -> list[JobRecord]:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT record_json FROM jobs WHERE expires_at IS NOT NULL AND expires_at <= ? ORDER BY expires_at LIMIT ?",
                    (cutoff, limit),
                ).fetchall()
                return [JobRecord.model_validate_json(row["record_json"]) for row in rows]

        async with self._lock:
            return await asyncio.to_thread(list_sync)

    async def mark_interrupted_failed(self) -> int:
        """Fail only running jobs left behind by a process restart."""

        await self._ensure_initialized()

        def mark_sync() -> int:
            count = 0
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute("SELECT record_json FROM jobs WHERE status=?", (JobStatus.RUNNING.value,)).fetchall()
                for row in rows:
                    job = JobRecord.model_validate_json(row["record_json"])
                    now = _utc_now()
                    job.status = JobStatus.FAILED
                    job.error = JobError(code="server_restarted", message="server restarted while the job was running")
                    job.updated_at = now
                    job.completed_at = now
                    connection.execute(self._insert_sql(replace=True), self._values(job))
                    count += 1
            return count

        async with self._lock:
            return await asyncio.to_thread(mark_sync)

    async def list(self, *, status: JobStatus | str | None = None, limit: int = 100) -> list[JobRecord]:
        await self._ensure_initialized()

        def list_sync() -> list[JobRecord]:
            sql = "SELECT record_json FROM jobs"
            values: tuple[object, ...] = ()
            if status is not None:
                sql += " WHERE status=?"
                values = (JobStatus(status).value,)
            sql += " ORDER BY created_at DESC LIMIT ?"
            values += (limit,)
            with self._connect() as connection:
                rows = connection.execute(sql, values).fetchall()
                return [JobRecord.model_validate_json(row["record_json"]) for row in rows]

        async with self._lock:
            return await asyncio.to_thread(list_sync)

    async def ping(self) -> bool:
        """Verify that the primary database/WAL can acquire a write transaction.

        A read-only ``SELECT 1`` can succeed even when jobs cannot be persisted,
        which would make /ready lie about operational readiness.
        """

        try:
            await self._ensure_initialized()

            def ping_sync() -> bool:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE jobs SET updated_at=updated_at WHERE 0")
                    connection.rollback()
                    return True
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()

            async with self._lock:
                return await asyncio.to_thread(ping_sync)
        except (OSError, sqlite3.Error):
            return False
