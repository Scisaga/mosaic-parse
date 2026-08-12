import pytest

from app.security.source_url import SourceUrlError, validate_redirect_url, validate_source_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/document.pdf",
        "http://127.0.0.1/document.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://100.100.100.200/latest/meta-data",
        "http://user:password@8.8.8.8/document.pdf",
        "http://10.0.0.1/document.pdf",
    ],
)
async def test_unsafe_source_urls_are_rejected(url: str) -> None:
    with pytest.raises(SourceUrlError):
        await validate_source_url(url)


async def test_public_literal_ip_is_allowed() -> None:
    result = await validate_source_url("https://8.8.8.8/document.pdf?download=1#fragment")
    assert result.hostname == "8.8.8.8"
    assert result.url == "https://8.8.8.8/document.pdf?download=1"


async def test_private_url_requires_explicit_opt_in() -> None:
    result = await validate_source_url("http://10.0.0.1/report.pdf", allow_private=True)
    assert result.addresses == ("10.0.0.1",)


async def test_each_redirect_is_revalidated() -> None:
    with pytest.raises(SourceUrlError):
        await validate_redirect_url("https://8.8.8.8/report.pdf", "http://127.0.0.1/private")

