#!/usr/bin/env python3
"""Irreversibly purge MosaicParse Job data while preserving the current schema."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

CONFIRMATION = "PURGE_MOSAICPARSE_JOBS_0_4_0"


class PurgeSafetyError(RuntimeError):
    """Raised when a destructive-operation guard is not satisfied."""


@dataclass(frozen=True, slots=True)
class PurgeReport:
    data_dir: str
    database_rows: int
    job_directories: int
    job_files: int
    job_bytes: int
    legacy_database_files: int


def service_is_active(health_url: str, *, timeout: float = 0.75) -> bool:
    request = Request(
        health_url,
        method="GET",
        headers={"User-Agent": "mosaicparse-purge/0.4.0"},
    )
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - operator URL
            return 200 <= int(response.status) < 500
    except HTTPError:
        return True
    except (OSError, URLError):
        return False


def validate_data_dir(value: str | Path) -> tuple[Path, Path, Path]:
    supplied = Path(value)
    if not supplied.is_absolute():
        raise PurgeSafetyError("data directory must be an absolute path")
    if supplied.is_symlink():
        raise PurgeSafetyError("data directory must not be a symbolic link")
    try:
        data_dir = supplied.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PurgeSafetyError("data directory does not exist") from exc
    if data_dir == Path("/") or len(data_dir.parts) < 3:
        raise PurgeSafetyError("refusing unsafe data directory")

    database = data_dir / "jobs.db"
    jobs_dir = data_dir / "jobs"
    if database.is_symlink() or not database.is_file():
        raise PurgeSafetyError("expected a regular jobs.db file")
    if jobs_dir.is_symlink() or not jobs_dir.is_dir():
        raise PurgeSafetyError("expected a regular jobs directory")
    return data_dir, database, jobs_dir


def _measure(path: Path) -> tuple[int, int]:
    if path.is_file() and not path.is_symlink():
        return 1, path.stat().st_size
    if not path.is_dir() or path.is_symlink():
        return 0, 0
    files = 0
    size = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            files += 1
            size += item.stat().st_size
    return files, size


def purge_data_dir(value: str | Path) -> PurgeReport:
    data_dir, database, jobs_dir = validate_data_dir(value)
    children = sorted(jobs_dir.iterdir(), key=lambda item: item.name)
    job_directories = sum(child.is_dir() and not child.is_symlink() for child in children)
    job_files = 0
    job_bytes = 0
    for child in children:
        files, size = _measure(child)
        job_files += files
        job_bytes += size

    # Files are removed before the database transaction. A filesystem failure therefore
    # leaves the durable index untouched and makes the failed purge obvious and retryable.
    for child in children:
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            raise PurgeSafetyError(f"unsupported Job entry: {child.name}")

    legacy_database_files = 0
    for name in ("jobs.sqlite3", "jobs.sqlite3-wal", "jobs.sqlite3-shm"):
        path = data_dir / name
        if path.is_symlink():
            raise PurgeSafetyError(f"refusing symbolic link: {name}")
        if path.exists():
            if not path.is_file():
                raise PurgeSafetyError(f"legacy database path is not a file: {name}")
            path.unlink()
            legacy_database_files += 1

    connection = sqlite3.connect(database, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        database_rows = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        connection.execute("DELETE FROM jobs")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    return PurgeReport(
        data_dir=str(data_dir),
        database_rows=database_rows,
        job_directories=job_directories,
        job_files=job_files,
        job_bytes=job_bytes,
        legacy_database_files=legacy_database_files,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", action="append", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--health-url", default="http://127.0.0.1:12303/health")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.confirm != CONFIRMATION:
            raise PurgeSafetyError(f"confirmation must exactly equal {CONFIRMATION}")
        if service_is_active(args.health_url):
            raise PurgeSafetyError("MosaicParse is still reachable; stop the main service first")
        reports = [purge_data_dir(value) for value in args.data_dir]
    except (OSError, sqlite3.Error, PurgeSafetyError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "purged",
                "confirmation": CONFIRMATION,
                "targets": [asdict(item) for item in reports],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
