"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AliasChoices, BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_to_none(value: object) -> object:
    if value == "":
        return None
    return value


OptionalSecret = Annotated[str | None, BeforeValidator(_empty_to_none)]


class Settings(BaseSettings):
    """Validated runtime settings.

    Environment names are the upper-case forms of these fields. Secrets are
    deliberately excluded from :meth:`public_config`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Docling GLM"
    app_id: str = "docling-glm-parser"
    version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = Field(default=12303, ge=1, le=65535)
    tz: str = "Asia/Shanghai"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = False
    data_dir: Path = Path("data")
    static_dir: Path = Path("static")

    api_key: OptionalSecret = None
    admin_token: str = "change-me"
    cors_origins: str = ""

    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    max_document_pages: int = Field(default=1000, gt=0)
    sync_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    sync_max_pages: int = Field(default=10, gt=0)
    max_queued_jobs: int = Field(default=8, ge=1)
    parser_workers: int = Field(default=1, ge=1, le=32)
    job_retention_hours: int = Field(default=24, ge=1)
    document_timeout_seconds: int = Field(default=900, ge=1)
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    backend_health_ttl_seconds: float = Field(default=15.0, ge=0)

    docling_device: str = "cpu"
    # Prefer the application-specific name: upstream Docling also consumes
    # DOCLING_ARTIFACTS_PATH and treats even an empty directory as offline-only.
    docling_artifacts_path: Path = Field(
        default=Path("/models/docling"),
        validation_alias=AliasChoices(
            "docling_artifacts_path",
            "DOCLING_LOCAL_ARTIFACTS_PATH",
            "DOCLING_ARTIFACTS_PATH",
        ),
    )
    docling_table_mode: Literal["fast", "accurate"] = "accurate"
    docling_force_backend_text: bool = False
    docling_model_download: bool = True
    docling_compile_models: bool = False

    glm_ocr_enabled: bool = True
    glm_ocr_api_url: str = "http://glm-ocr:8000/v1/chat/completions"
    glm_ocr_api_key: OptionalSecret = None
    glm_ocr_model: str = "zai-org/GLM-OCR"
    glm_ocr_lang: str = "zh,en"
    glm_ocr_scale: float = Field(default=3.0, gt=0)
    glm_ocr_max_image_pixels: int = Field(default=4_500_000, gt=0)
    glm_ocr_max_concurrency: int = Field(default=4, ge=1)
    # This is an output-token budget, not the model context window.  Keep
    # enough room for the image and prompt when vLLM uses a 16k context.
    glm_ocr_max_tokens: int = Field(default=4_096, ge=1)
    # GLM-OCR accepts a small fixed prompt vocabulary.  Its general full-page
    # OCR contract is more reliable than the plugin's free-form default.
    glm_ocr_prompt: str = Field(default="Text Recognition:", min_length=1)
    glm_ocr_timeout_seconds: float = Field(default=120.0, gt=0)
    glm_ocr_max_retries: int = Field(default=3, ge=0)

    glm_sdk_enabled: bool = False
    glm_sdk_url: str = "http://glm-ocr-sdk:5002/glmocr/parse"

    vlm_enabled: bool = False
    vlm_base_url: str = "http://5090-host:11434/v1"
    vlm_api_key: OptionalSecret = "ollama"
    vlm_model: str = "qwen3.6:27b"
    vlm_max_concurrency: int = Field(default=1, ge=1)
    vlm_timeout_seconds: float = Field(default=300.0, gt=0)
    vlm_temperature: float = Field(default=0.0, ge=0)
    vlm_max_retries: int = Field(default=1, ge=0)
    # Independently switchable for remote-call cost/privacy control. The
    # service also requires vlm_enabled before making an enrichment request.
    vlm_diagram_enrichment_enabled: bool = True

    allow_source_urls: bool = True
    allow_private_source_urls: bool = False
    source_url_max_redirects: int = Field(default=3, ge=0, le=20)
    source_url_timeout_seconds: float = Field(default=30.0, gt=0)
    source_url_user_agent: str = "docling-glm-parser/0.1"

    mcp_enabled: bool = True
    mcp_max_inline_bytes: int = Field(default=2 * 1024 * 1024, gt=0)
    mcp_max_result_chars: int = Field(default=200_000, gt=0)
    mcp_allowed_hosts: str = "localhost,127.0.0.1,[::1],parser,docling_glm_parser"
    mcp_allowed_origins: str = ""

    @field_validator("docling_device")
    @classmethod
    def validate_docling_device(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "auto" or normalized == "cpu" or normalized == "mps":
            return normalized
        if normalized == "cuda" or normalized.startswith("cuda:"):
            if normalized != "cuda":
                _, _, index = normalized.partition(":")
                if not index.isdigit():
                    raise ValueError("DOCLING_DEVICE must be cpu, auto, mps, cuda, or cuda:<index>")
            return normalized
        raise ValueError("DOCLING_DEVICE must be cpu, auto, mps, cuda, or cuda:<index>")

    @field_validator("glm_ocr_api_url", "glm_sdk_url", "vlm_base_url")
    @classmethod
    def validate_backend_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("backend URL must be an absolute HTTP(S) URL")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_limits(self) -> Settings:
        if self.sync_max_bytes > self.max_upload_bytes:
            raise ValueError("SYNC_MAX_BYTES cannot exceed MAX_UPLOAD_BYTES")
        if self.sync_max_pages > self.max_document_pages:
            raise ValueError("SYNC_MAX_PAGES cannot exceed MAX_DOCUMENT_PAGES")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def default_languages(self) -> list[str]:
        return [language.strip() for language in self.glm_ocr_lang.split(",") if language.strip()]

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def mcp_allowed_host_list(self) -> list[str]:
        hosts = [item.strip() for item in self.mcp_allowed_hosts.split(",") if item.strip()]
        expanded: list[str] = []
        for host in hosts:
            expanded.append(host)
            if ":" not in host or host.startswith("["):
                expanded.append(f"{host}:*")
        return list(dict.fromkeys(expanded))

    @property
    def mcp_allowed_origin_list(self) -> list[str]:
        return [item.strip() for item in self.mcp_allowed_origins.split(",") if item.strip()]

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.db"

    def public_config(self) -> dict[str, object]:
        """Return only non-sensitive operational limits for health responses."""

        return {
            "max_upload_bytes": self.max_upload_bytes,
            "max_document_pages": self.max_document_pages,
            "sync_max_bytes": self.sync_max_bytes,
            "sync_max_pages": self.sync_max_pages,
            "max_queued_jobs": self.max_queued_jobs,
            "parser_workers": self.parser_workers,
            "job_retention_hours": self.job_retention_hours,
            "document_timeout_seconds": self.document_timeout_seconds,
            "docling_device": self.docling_device,
            "source_urls_enabled": self.allow_source_urls,
            "private_source_urls_allowed": self.allow_private_source_urls,
            "glm_ocr_enabled": self.glm_ocr_enabled,
            "vlm_enabled": self.vlm_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings (used by tests and explicit admin reloads)."""

    get_settings.cache_clear()
