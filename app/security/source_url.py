"""SSRF checks used before every URL request and redirect hop."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data",
}
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}


class SourceUrlError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedSourceUrl:
    url: str
    hostname: str
    addresses: tuple[str, ...]


def _normalized_url(url: str) -> tuple[str, str, int | None]:
    if not isinstance(url, str) or not url.strip():
        raise SourceUrlError("invalid_source_url", "source URL is empty")
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise SourceUrlError(
            "invalid_source_url", "source URL has an invalid port or authority"
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceUrlError(
            "invalid_source_url_scheme", "only HTTP and HTTPS source URLs are allowed"
        )
    if not parsed.hostname:
        raise SourceUrlError("invalid_source_url", "source URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SourceUrlError(
            "source_url_credentials_forbidden", "credentials in source URLs are not allowed"
        )
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in _BLOCKED_HOSTNAMES or hostname.endswith(".localhost"):
        raise SourceUrlError("source_url_blocked", "localhost and metadata hosts are not allowed")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SourceUrlError("invalid_source_url", "source URL hostname is invalid") from exc
    netloc = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    normalized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))
    return normalized, ascii_hostname, port


def is_public_address(address: str, *, allow_private: bool = False) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip in _METADATA_IPS:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    if not allow_private and (ip.is_private or not ip.is_global):
        return False
    return True


async def _resolve(hostname: str, port: int | None) -> tuple[str, ...]:
    def lookup() -> tuple[str, ...]:
        infos = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
        return tuple(sorted({str(info[4][0]).split("%", 1)[0] for info in infos}))

    try:
        return await asyncio.to_thread(lookup)
    except socket.gaierror as exc:
        raise SourceUrlError(
            "source_url_dns_failed", "source URL hostname could not be resolved"
        ) from exc


async def validate_source_url(url: str, *, allow_private: bool = False) -> ValidatedSourceUrl:
    normalized, hostname, port = _normalized_url(url)
    addresses: tuple[str, ...]
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = (str(literal),)
    except ValueError:
        addresses = await _resolve(hostname, port)
    if not addresses:
        raise SourceUrlError(
            "source_url_dns_failed", "source URL hostname resolved to no addresses"
        )
    blocked = [
        address
        for address in addresses
        if not is_public_address(address, allow_private=allow_private)
    ]
    # Reject the whole hostname if any answer is unsafe; accepting just one answer
    # leaves room for resolver/load-balancer based SSRF bypasses.
    if blocked:
        raise SourceUrlError(
            "source_url_blocked", "source URL resolves to a prohibited network address"
        )
    return ValidatedSourceUrl(url=normalized, hostname=hostname, addresses=addresses)


async def validate_redirect_url(
    current_url: str,
    location: str,
    *,
    allow_private: bool = False,
) -> ValidatedSourceUrl:
    if not location or not location.strip():
        raise SourceUrlError("invalid_redirect", "redirect response has no Location")
    return await validate_source_url(urljoin(current_url, location), allow_private=allow_private)
