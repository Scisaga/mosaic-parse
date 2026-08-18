"""Constant-time token verification helpers."""

from __future__ import annotations

import hmac

from app.models.error import ServiceError


def extract_bearer_token(authorization: str | None, x_api_key: str | None = None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return None


def token_matches(provided: str | None, configured: str | None) -> bool:
    if not configured:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))


def require_api_key(provided: str | None, configured: str | None) -> None:
    if not token_matches(provided, configured):
        raise ServiceError("invalid_api_key", "invalid API key", status_code=401)


def require_admin_token(provided: str | None, configured: str | None) -> None:
    if not configured:
        raise ServiceError("admin_disabled", "admin endpoints require ADMIN_TOKEN", status_code=503)
    if not token_matches(provided, configured):
        raise ServiceError("invalid_admin_token", "invalid admin token", status_code=401)
