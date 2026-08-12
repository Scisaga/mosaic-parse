"""Sortable, opaque identifiers without an additional ULID dependency."""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        chars[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(chars)


def new_id(prefix: str) -> str:
    """Return a ULID-shaped, lexicographically time-sortable identifier."""

    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = secrets.randbits(80)
    return f"{prefix}_{_encode_crockford(timestamp_ms, 10)}{_encode_crockford(randomness, 16)}"


def new_document_id() -> str:
    return new_id("docparse")


def new_job_id() -> str:
    return new_id("job")


def new_request_id() -> str:
    return new_id("req")

