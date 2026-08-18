from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import purge_job_data


def data_store(tmp_path: Path) -> Path:
    root = tmp_path / "mosaicparse-data"
    jobs = root / "jobs"
    job = jobs / "job_fixture"
    (job / "output").mkdir(parents=True)
    (job / "output" / "result.json").write_bytes(b"result")
    (root / "models").mkdir()
    (root / "models" / "cache.bin").write_bytes(b"model-cache")
    (root / "jobs.sqlite3").write_bytes(b"legacy")
    connection = sqlite3.connect(root / "jobs.db")
    connection.executescript(
        """
        CREATE TABLE jobs (id TEXT PRIMARY KEY);
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO jobs(id) VALUES ('job_fixture');
        INSERT INTO schema_migrations(version, name) VALUES (1, 'initial');
        """
    )
    connection.close()
    return root


def row_count(root: Path, table: str) -> int:
    query = {
        "jobs": "SELECT COUNT(*) FROM jobs",
        "schema_migrations": "SELECT COUNT(*) FROM schema_migrations",
    }[table]
    connection = sqlite3.connect(root / "jobs.db")
    try:
        return int(connection.execute(query).fetchone()[0])
    finally:
        connection.close()


def test_purge_removes_jobs_and_legacy_database_but_preserves_schema_and_cache(
    tmp_path: Path,
) -> None:
    root = data_store(tmp_path)

    report = purge_job_data.purge_data_dir(root)

    assert report.database_rows == 1
    assert report.job_directories == 1
    assert report.job_files == 1
    assert report.job_bytes == len(b"result")
    assert report.legacy_database_files == 1
    assert list((root / "jobs").iterdir()) == []
    assert row_count(root, "jobs") == 0
    assert row_count(root, "schema_migrations") == 1
    assert (root / "models" / "cache.bin").read_bytes() == b"model-cache"
    assert not (root / "jobs.sqlite3").exists()

    repeated = purge_job_data.purge_data_dir(root)
    assert repeated.database_rows == 0
    assert repeated.job_directories == 0
    assert repeated.job_files == 0


def test_purge_refuses_root_relative_and_symbolic_link_targets(tmp_path: Path) -> None:
    with pytest.raises(purge_job_data.PurgeSafetyError):
        purge_job_data.validate_data_dir(Path("/"))
    with pytest.raises(purge_job_data.PurgeSafetyError):
        purge_job_data.validate_data_dir(Path("relative-data"))

    root = data_store(tmp_path)
    link = tmp_path / "linked-data"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(purge_job_data.PurgeSafetyError):
        purge_job_data.validate_data_dir(link)


def test_purge_stops_before_database_change_when_file_removal_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = data_store(tmp_path)

    def fail(_path: Path) -> None:
        raise OSError("simulated removal failure")

    monkeypatch.setattr(purge_job_data.shutil, "rmtree", fail)
    with pytest.raises(OSError, match="simulated removal failure"):
        purge_job_data.purge_data_dir(root)
    assert row_count(root, "jobs") == 1


def test_cli_requires_confirmation_and_stopped_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = data_store(tmp_path)
    assert purge_job_data.main(["--data-dir", str(root), "--confirm", "wrong"]) == 2
    assert "confirmation must exactly equal" in capsys.readouterr().err

    monkeypatch.setattr(purge_job_data, "service_is_active", lambda _url: True)
    assert purge_job_data.main(
        ["--data-dir", str(root), "--confirm", purge_job_data.CONFIRMATION]
    ) == 2
    assert "still reachable" in capsys.readouterr().err
    assert row_count(root, "jobs") == 1
