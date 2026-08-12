"""Scoped, atomic local job storage."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.models.parse_result import DocumentParseResult
from app.models.source import StoredSource
from app.security.file_validation import FileValidationError, safe_filename, validate_stored_file
from app.utils.settings import setting, setting_path

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class StoredResultPaths:
    markdown: Path
    text: Path
    metadata: Path
    warnings: Path


class StorageService:
    def __init__(self, settings: object | None = None) -> None:
        self.settings = settings
        self.data_dir = setting_path(settings, "data_dir", "data").resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.max_upload_bytes = int(setting(settings, "max_upload_bytes", 200 * 1024 * 1024))
        self.max_document_pages = int(setting(settings, "max_document_pages", 1_000))

    async def initialize(self) -> None:
        await asyncio.to_thread(self.jobs_dir.mkdir, parents=True, exist_ok=True)

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if not _SAFE_ID_RE.fullmatch(job_id):
            raise ValueError("invalid job identifier")
        return job_id

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / self._validate_job_id(job_id)

    def input_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "input"

    def output_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "output"

    def logs_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "logs"

    async def create_job_layout(self, job_id: str) -> Path:
        root = self.job_dir(job_id)

        def create() -> None:
            jobs_root = self.jobs_dir.resolve()
            resolved = root.resolve(strict=False)
            if resolved.parent != jobs_root or root.is_symlink():
                raise OSError("job storage path escapes the configured jobs directory")
            root.mkdir(parents=True, exist_ok=True)
            for directory in (self.input_dir(job_id), self.output_dir(job_id), self.logs_dir(job_id)):
                if directory.is_symlink():
                    raise OSError("job storage subdirectory must not be a symbolic link")
                directory.mkdir(parents=True, exist_ok=True)

        await asyncio.to_thread(create)
        return root

    async def _iter_stream(self, stream: object, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        while True:
            result = stream.read(chunk_size)  # type: ignore[attr-defined]
            chunk = await result if inspect.isawaitable(result) else result
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                raise TypeError("binary source stream returned non-bytes data")
            yield bytes(chunk)

    async def save_stream(
        self,
        job_id: str,
        filename: str,
        stream: BinaryIO | object,
        *,
        source_url: str | None = None,
    ) -> StoredSource:
        return await self.save_chunks(job_id, filename, self._iter_stream(stream), source_url=source_url)

    async def save_bytes(
        self,
        job_id: str,
        filename: str,
        content: bytes,
        *,
        source_url: str | None = None,
    ) -> StoredSource:
        async def chunks() -> AsyncIterator[bytes]:
            yield content

        return await self.save_chunks(job_id, filename, chunks(), source_url=source_url)

    async def save_chunks(
        self,
        job_id: str,
        filename: str,
        chunks: AsyncIterable[bytes],
        *,
        source_url: str | None = None,
    ) -> StoredSource:
        await self.create_job_layout(job_id)
        input_dir = self.input_dir(job_id)
        temporary = input_dir / ".upload.part"
        total = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise FileValidationError(
                            "file_too_large",
                            f"file exceeds maximum size of {self.max_upload_bytes} bytes",
                        )
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
                await asyncio.to_thread(os.fsync, handle.fileno())

            cleaned, mime_type, size, page_count = await asyncio.to_thread(
                validate_stored_file,
                temporary,
                filename,
                max_bytes=self.max_upload_bytes,
                max_pages=self.max_document_pages,
            )
            target = input_dir / safe_filename(cleaned)
            if target.exists():
                target = input_dir / f"original{target.suffix}"
            await asyncio.to_thread(os.replace, temporary, target)
            return StoredSource(
                path=target,
                filename=cleaned,
                mime_type=mime_type,
                size_bytes=size,
                page_count=page_count,
                source_url=source_url,
            )
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def write_result(self, job_id: str, result: DocumentParseResult) -> StoredResultPaths:
        await self.create_job_layout(job_id)
        output = self.output_dir(job_id)
        paths = StoredResultPaths(
            markdown=output / "result.md",
            text=output / "result.txt",
            metadata=output / "metadata.json",
            warnings=self.logs_dir(job_id) / "warnings.json",
        )
        warning_payload = {
            "document_warnings": [warning.model_dump(mode="json") for warning in result.warnings],
            "pages": [
                {
                    "page_number": page.page_number,
                    "status": page.status.value,
                    "warnings": [warning.model_dump(mode="json") for warning in page.warnings],
                }
                for page in result.pages
                if page.warnings
            ],
        }
        await asyncio.gather(
            asyncio.to_thread(self._atomic_write, paths.markdown, result.markdown.encode("utf-8")),
            asyncio.to_thread(self._atomic_write, paths.text, result.plain_text.encode("utf-8")),
            asyncio.to_thread(
                self._atomic_write,
                paths.metadata,
                result.model_dump_json(indent=2).encode("utf-8"),
            ),
            asyncio.to_thread(
                self._atomic_write,
                paths.warnings,
                json.dumps(warning_payload, ensure_ascii=False, indent=2).encode("utf-8"),
            ),
        )
        return paths

    def result_path(self, job_id: str, output_format: str = "markdown") -> Path | None:
        filename = "result.txt" if str(output_format) == "text" else "result.md"
        path = self.output_dir(job_id) / filename
        return path if path.is_file() else None

    async def read_result(self, job_id: str, output_format: str = "markdown") -> str:
        path = self.result_path(job_id, output_format)
        if path is None:
            raise FileNotFoundError(job_id)
        return await asyncio.to_thread(path.read_text, encoding="utf-8")

    async def read_metadata(self, job_id: str) -> DocumentParseResult:
        path = self.output_dir(job_id) / "metadata.json"
        if not path.is_file():
            raise FileNotFoundError(job_id)
        data = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return DocumentParseResult.model_validate_json(data)

    async def copy_source(self, source: StoredSource, target_job_id: str) -> StoredSource:
        async def chunks() -> AsyncIterator[bytes]:
            with source.path.open("rb") as handle:
                while chunk := await asyncio.to_thread(handle.read, 1024 * 1024):
                    yield chunk

        return await self.save_chunks(
            target_job_id,
            source.filename,
            chunks(),
            source_url=source.source_url,
        )

    async def delete_job(self, job_id: str) -> bool:
        root = self.job_dir(job_id)
        if not root.exists():
            return False
        if root.is_symlink():
            await asyncio.to_thread(root.unlink)
        else:
            await asyncio.to_thread(shutil.rmtree, root)
        return True

    async def is_writable(self) -> bool:
        try:
            await self.initialize()

            def probe() -> bool:
                descriptor, name = tempfile.mkstemp(prefix=".write-probe-", dir=self.data_dir)
                os.close(descriptor)
                Path(name).unlink()
                return True

            return await asyncio.to_thread(probe)
        except OSError:
            return False
