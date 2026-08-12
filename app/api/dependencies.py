"""FastAPI request dependencies and authentication helpers."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Annotated

from fastapi import Header, Request

from app.models import ServiceError

if TYPE_CHECKING:
    from app.lifespan import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise ServiceError(
            "service_not_ready",
            "The parser service is still starting",
            status_code=503,
        )
    return runtime


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and token:
        return token.strip()
    return None


def _matches(candidate: str | None, expected: str) -> bool:
    if candidate is None or not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


async def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    """Require the optional public API key without leaking comparison details."""

    expected = request.app.state.settings.api_key
    if expected is None:
        return
    if _matches(x_api_key, expected) or _matches(_bearer_token(authorization), expected):
        return
    raise ServiceError("invalid_api_key", "A valid API key is required", status_code=401)


async def require_admin_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
) -> None:
    """Protect state-changing administrative endpoints with a separate token."""

    expected = request.app.state.settings.admin_token
    if _matches(x_admin_token, expected) or _matches(_bearer_token(authorization), expected):
        return
    raise ServiceError("invalid_admin_token", "A valid admin token is required", status_code=401)
