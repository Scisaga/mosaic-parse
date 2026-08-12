from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def make_settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_public_config_never_contains_secrets(tmp_path: Path) -> None:
    settings = make_settings(
        data_dir=tmp_path,
        api_key="public-secret",
        admin_token="admin-secret",
        glm_ocr_api_key="glm-secret",
        vlm_api_key="vlm-secret",
    )

    serialized = repr(settings.public_config())
    assert "public-secret" not in serialized
    assert "admin-secret" not in serialized
    assert "glm-secret" not in serialized
    assert "vlm-secret" not in serialized


@pytest.mark.parametrize("device", ["cpu", "auto", "mps", "cuda", "cuda:1"])
def test_docling_device_accepts_supported_values(device: str) -> None:
    assert make_settings(docling_device=device).docling_device == device


def test_docling_device_rejects_invalid_index() -> None:
    with pytest.raises(ValidationError):
        make_settings(docling_device="cuda:gpu-1")


def test_sync_limits_cannot_exceed_global_limits() -> None:
    with pytest.raises(ValidationError, match="SYNC_MAX_BYTES"):
        make_settings(max_upload_bytes=100, sync_max_bytes=101)
    with pytest.raises(ValidationError, match="SYNC_MAX_PAGES"):
        make_settings(max_document_pages=2, sync_max_pages=3)


def test_mcp_host_allowlist_adds_port_patterns() -> None:
    settings = make_settings(mcp_allowed_hosts="localhost,parser:12303")
    assert settings.mcp_allowed_host_list == ["localhost", "localhost:*", "parser:12303"]

