import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.models import ServiceError
from app.services.source_service import SourceService
from app.services.storage_service import StorageService


def settings(tmp_path: Path, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "max_upload_bytes": 1_000_000,
        "max_content_units": 10,
        "allow_source_urls": True,
        "allow_private_source_urls": False,
        "source_url_max_redirects": 2,
        "source_url_timeout_seconds": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_url_source_is_streamed_and_content_validated(tmp_path: Path) -> None:
    content = Path("tests/fixtures/native-report.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/pdf,image/*"
        return httpx.Response(
            200,
            headers={
                "Content-Length": str(len(content)),
                "Content-Disposition": 'attachment; filename="remote-report.pdf"',
            },
            content=content,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configuration = settings(tmp_path)
    storage = StorageService(configuration)
    service = SourceService(configuration, storage, client)
    source = await service.from_url("job_urltest", "https://8.8.8.8/report.pdf")
    assert source.filename == "remote-report.pdf"
    assert source.mime_type == "application/pdf"
    assert source.page_count == 1
    assert source.source_url == "https://8.8.8.8/report.pdf"
    await client.aclose()


async def test_hostname_is_resolved_once_then_request_is_ip_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = Path("tests/fixtures/native-report.pdf").read_bytes()
    resolutions = 0

    async def alternating_resolver(_hostname: str, _port: int | None) -> tuple[str, ...]:
        nonlocal resolutions
        resolutions += 1
        # A vulnerable check-then-use implementation would resolve again and
        # could receive an attacker-controlled loopback answer on the next call.
        return ("93.184.216.34",) if resolutions == 1 else ("127.0.0.1",)

    monkeypatch.setattr("app.security.source_url._resolve", alternating_resolver)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "safe.example"
        assert request.extensions["sni_hostname"] == "safe.example"
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configuration = settings(tmp_path)
    service = SourceService(configuration, StorageService(configuration), client)
    source = await service.from_url("job_pinned", "https://safe.example/report.pdf")
    assert source.source_url == "https://safe.example/report.pdf"
    assert resolutions == 1
    await client.aclose()


async def test_redirect_to_private_network_is_blocked(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "http://127.0.0.1/private.pdf"})
        )
    )
    configuration = settings(tmp_path)
    service = SourceService(configuration, StorageService(configuration), client)
    with pytest.raises(ServiceError) as caught:
        await service.from_url("job_redirect", "https://8.8.8.8/report.pdf")
    assert caught.value.code == "source_url_blocked"
    await client.aclose()


async def test_remote_content_length_is_limited_before_download(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"Content-Length": "2000"}, content=b"ignored")
        )
    )
    configuration = settings(tmp_path, max_upload_bytes=1000)
    service = SourceService(configuration, StorageService(configuration), client)
    with pytest.raises(ServiceError) as caught:
        await service.from_url("job_large", "https://8.8.8.8/report.pdf")
    assert caught.value.code == "file_too_large"
    assert caught.value.status_code == 413
    await client.aclose()


async def test_url_download_has_a_total_wall_clock_deadline(tmp_path: Path) -> None:
    content = Path("tests/fixtures/native-report.pdf").read_bytes()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield content

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowBody()))
    )
    configuration = settings(tmp_path, source_url_timeout_seconds=0.01)
    service = SourceService(configuration, StorageService(configuration), client)
    with pytest.raises(ServiceError) as caught:
        await service.from_url("job_slow_drip", "https://8.8.8.8/report.pdf")
    assert caught.value.code == "source_download_timeout"
    assert caught.value.status_code == 504
    await client.aclose()
