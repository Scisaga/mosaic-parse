"""Upload and URL materialization with size and SSRF enforcement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from app.models.error import ServiceError
from app.models.source import StoredSource
from app.security.file_validation import FileValidationError
from app.security.source_url import (
    SourceUrlError,
    ValidatedSourceUrl,
    validate_redirect_url,
    validate_source_url,
)
from app.services.storage_service import StorageService
from app.utils.settings import setting


class SourceService:
    def __init__(
        self,
        settings: object | None,
        storage: StorageService,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.allow_source_urls = bool(setting(settings, "allow_source_urls", True))
        self.allow_private_source_urls = bool(setting(settings, "allow_private_source_urls", False))
        self.max_redirects = int(setting(settings, "source_url_max_redirects", 3))
        self.timeout_seconds = float(setting(settings, "source_url_timeout_seconds", 30))
        self.user_agent = str(setting(settings, "source_url_user_agent", "docling-glm-parser/0.1"))
        self.max_upload_bytes = int(setting(settings, "max_upload_bytes", 200 * 1024 * 1024))
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(self.timeout_seconds),
            # Requests are pinned to validated IP literals. Avoid pooling a TLS
            # connection across two hostnames that happen to resolve to the same
            # address, because SNI is intentionally kept as the original host.
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=0),
            trust_env=False,
        )

    @staticmethod
    def _pinned_url(validated: ValidatedSourceUrl, address: str) -> str:
        parsed = urlsplit(validated.url)
        literal = f"[{address}]" if ":" in address else address
        if parsed.port is not None:
            literal = f"{literal}:{parsed.port}"
        return urlunsplit((parsed.scheme, literal, parsed.path, parsed.query, ""))

    async def _send_validated(self, validated: ValidatedSourceUrl) -> httpx.Response:
        """Connect only to an address returned by the completed SSRF check."""

        original_authority = urlsplit(validated.url).netloc
        last_error: httpx.HTTPError | None = None
        for address in validated.addresses:
            request = self._client.build_request(
                "GET",
                self._pinned_url(validated, address),
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/pdf,image/*",
                    "Host": original_authority,
                    "Connection": "close",
                },
                # httpcore uses this for TLS SNI and certificate verification,
                # while the TCP connection target remains the validated IP.
                extensions={"sni_hostname": validated.hostname},
            )
            try:
                return await self._client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpx.ConnectError("source URL resolved to no usable addresses")

    @asynccontextmanager
    async def _stream_validated(
        self,
        validated: ValidatedSourceUrl,
    ) -> AsyncIterator[httpx.Response]:
        response = await self._send_validated(validated)
        try:
            yield response
        finally:
            await response.aclose()

    @staticmethod
    def _map_validation_error(exc: FileValidationError) -> ServiceError:
        status = 413 if exc.code in {"file_too_large", "too_many_pages"} else 415
        if exc.code in {"empty_file", "empty_document", "invalid_pdf", "invalid_image"}:
            status = 400
        return ServiceError(exc.code, str(exc), status_code=status)

    async def from_upload(self, job_id: str, upload: object) -> StoredSource:
        filename = getattr(upload, "filename", None) or "document"
        try:
            return await self.storage.save_stream(job_id, filename, upload)
        except FileValidationError as exc:
            raise self._map_validation_error(exc) from exc

    async def from_bytes(self, job_id: str, filename: str, content: bytes) -> StoredSource:
        try:
            return await self.storage.save_bytes(job_id, filename, content)
        except FileValidationError as exc:
            raise self._map_validation_error(exc) from exc

    @staticmethod
    def _response_filename(response: httpx.Response, url: str) -> str:
        disposition = response.headers.get("content-disposition")
        if disposition:
            message = Message()
            message["content-disposition"] = disposition
            candidate = message.get_filename()
            if candidate:
                return candidate
        path_name = PurePosixPath(unquote(urlsplit(url).path)).name
        return path_name or "document"

    async def from_url(self, job_id: str, source_url: str) -> StoredSource:
        try:
            # httpx timeouts are per network phase/read operation. This outer
            # deadline also stops a slow-drip peer from occupying a download
            # slot indefinitely by sending one chunk before every read timeout.
            async with asyncio.timeout(self.timeout_seconds):
                return await self._from_url(job_id, source_url)
        except TimeoutError as exc:
            raise ServiceError(
                "source_download_timeout",
                "source URL download timed out",
                status_code=504,
            ) from exc

    async def _from_url(self, job_id: str, source_url: str) -> StoredSource:
        if not self.allow_source_urls:
            raise ServiceError("source_urls_disabled", "URL sources are disabled", status_code=400)
        try:
            validated = await validate_source_url(source_url, allow_private=self.allow_private_source_urls)
            for redirect_count in range(self.max_redirects + 1):
                async with self._stream_validated(validated) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= self.max_redirects:
                            raise ServiceError("too_many_redirects", "source URL exceeded redirect limit", status_code=400)
                        validated = await validate_redirect_url(
                            validated.url,
                            response.headers.get("location", ""),
                            allow_private=self.allow_private_source_urls,
                        )
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ServiceError(
                            "source_download_failed",
                            f"source server returned HTTP {response.status_code}",
                            status_code=502,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > self.max_upload_bytes:
                                raise ServiceError("file_too_large", "remote file exceeds the upload size limit", status_code=413)
                        except ValueError:
                            pass
                    filename = self._response_filename(response, validated.url)
                    try:
                        return await self.storage.save_chunks(
                            job_id,
                            filename,
                            response.aiter_bytes(1024 * 1024),
                            source_url=validated.url,
                        )
                    except FileValidationError as exc:
                        raise self._map_validation_error(exc) from exc
            raise ServiceError("too_many_redirects", "source URL exceeded redirect limit", status_code=400)
        except SourceUrlError as exc:
            raise ServiceError(exc.code, str(exc), status_code=400) from exc
        except httpx.TimeoutException as exc:
            raise ServiceError("source_download_timeout", "source URL download timed out", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise ServiceError("source_download_failed", "source URL could not be downloaded", status_code=502) from exc

    async def prepare(
        self,
        job_id: str,
        *,
        file: object | None = None,
        source_url: str | None = None,
        filename: str | None = None,
        content: bytes | None = None,
    ) -> StoredSource:
        choices = int(file is not None) + int(source_url is not None) + int(content is not None)
        if choices != 1:
            raise ServiceError(
                "source_conflict",
                "exactly one of file, source_url, or content must be provided",
                status_code=400,
            )
        if file is not None:
            return await self.from_upload(job_id, file)
        if source_url is not None:
            return await self.from_url(job_id, source_url)
        return await self.from_bytes(job_id, filename or "document", content or b"")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
