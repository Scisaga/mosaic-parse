import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.models import JobRecord, JobStatus
from app.models.job import utc_now
from app.repositories import JobRepository


def job_record(job_id: str = "job_01TEST") -> JobRecord:
    return JobRecord(
        id=job_id,
        filename="report.pdf",
        mime_type="application/pdf",
        input_bytes=123,
        page_count=2,
        source_path="/tmp/report.pdf",
    )


async def test_repository_persists_state_and_progress(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    await repository.initialize()
    await repository.create(job_record())
    running = await repository.transition("job_01TEST", JobStatus.RUNNING)
    assert running.started_at is not None
    progressed = await repository.update_progress("job_01TEST", 1, 2)
    assert progressed.progress.current == 1
    completed = await repository.complete(
        "job_01TEST",
        result_path="result.json",
        markdown_path="result.md",
        text_path="result.txt",
    )
    assert completed.status == JobStatus.COMPLETED
    assert (await repository.get("job_01TEST")) == completed


async def test_invalid_state_transition_is_rejected(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    await repository.create(job_record())
    await repository.cancel("job_01TEST")
    with pytest.raises(ValueError, match="invalid job transition"):
        await repository.transition("job_01TEST", JobStatus.RUNNING)


async def test_restart_marks_running_job_failed(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    await repository.create(job_record())
    await repository.transition("job_01TEST", JobStatus.RUNNING)
    assert await repository.mark_interrupted_failed() == 1
    restored = await repository.require("job_01TEST")
    assert restored.status == JobStatus.FAILED
    assert restored.error is not None
    assert restored.error.code == "server_restarted"


async def test_expiration_query(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    record = job_record()
    record.expires_at = utc_now() - timedelta(seconds=1)
    await repository.create(record)
    assert [item.id for item in await repository.list_expired()] == [record.id]


async def test_ping_requires_a_writable_database_transaction(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "jobs.db")
    await repository.initialize()
    assert await repository.ping() is True

    def readonly_connect() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{repository.db_path}?mode=ro", uri=True)

    repository._connect = readonly_connect  # type: ignore[method-assign]
    assert await repository.ping() is False


async def test_versioned_migration_marks_active_legacy_job_interrupted(tmp_path: Path) -> None:
    database = tmp_path / "jobs.db"
    repository = JobRepository(database)
    await repository.initialize()
    await repository.create(job_record())
    await repository.transition("job_01TEST", JobStatus.RUNNING)

    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT record_json FROM jobs WHERE id=?", ("job_01TEST",)
            ).fetchone()[0]
        )
        payload["object"] = "document.parse.job"
        payload["options"]["page_range"] = payload["options"].pop("unit_range")
        payload["options"].update(
            {
                "mode": "auto",
                "output_format": "markdown",
                "enable_vlm_fallback": False,
                "preserve_page_breaks": True,
                "include_pages": True,
                "include_diagnostics": True,
            }
        )
        payload["metadata_path"] = "/data/jobs/job_01TEST/output/metadata.json"
        connection.execute(
            "UPDATE jobs SET record_json=? WHERE id=?",
            (json.dumps(payload), "job_01TEST"),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version=2")

    migrated = JobRepository(database)
    await migrated.initialize()
    restored = await migrated.require("job_01TEST")
    assert restored.object == "content.parse.job"
    assert restored.status == JobStatus.FAILED
    assert restored.error is not None
    assert restored.error.code == "legacy_interruption"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM schema_migrations WHERE version=2"
        ).fetchone()[0] == "mosaicparse_content_contract"
