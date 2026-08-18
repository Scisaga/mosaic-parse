"""Remote GLM-OCR capability probe and Docling plugin option factory."""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.models.backend import BackendState, BackendStatus
from app.utils.settings import setting

if TYPE_CHECKING:
    from docling_glm_ocr import GlmOcrRemoteOptions


def _install_plugin_compatibility() -> None:
    """Bridge docling-glm-ocr 0.5.0 to current Docling safely.

    The locked plugin still calls the pre-2.119 ``post_process_cells``
    signature, creates an httpx client that inherits ambient proxy variables,
    and sends multimodal message parts in an order that drops text with current
    GLM-OCR checkpoints. Keep the shim isolated at this third-party boundary
    until the upstream plugin ships the corresponding fixes.
    """

    from docling.models.base_ocr_model import BaseOcrModel
    from docling_glm_ocr import GlmOcrRemoteModel

    if getattr(GlmOcrRemoteModel, "_mosaicparse_compat", False):
        return

    original_call = GlmOcrRemoteModel.__call__

    def compatible_call(self: Any, conv_res: Any, page_batch: Iterable[Any]) -> Iterable[Any]:
        self._local.mosaicparse_conv_res = conv_res
        try:
            yield from original_call(self, conv_res, page_batch)
        finally:
            self._local.mosaicparse_conv_res = None

    def compatible_post_process(
        self: Any,
        ocr_cells: list[Any],
        page: Any,
        conv_res: Any | None = None,
        priority: Any | None = None,
    ) -> None:
        actual_conversion = conv_res or getattr(self._local, "mosaicparse_conv_res", None)
        if actual_conversion is None:
            raise RuntimeError("GLM-OCR post-processing has no active conversion result")
        BaseOcrModel.post_process_cells(self, ocr_cells, page, actual_conversion, priority)

    def compatible_get_client(self: Any) -> httpx.Client:
        if not self.enabled:
            raise RuntimeError("GlmOcrRemoteModel is not enabled")
        if not hasattr(self._local, "client"):
            limits = httpx.Limits(
                max_connections=self.options.max_concurrent_requests,
                max_keepalive_connections=self.options.max_concurrent_requests,
            )
            headers: dict[str, str] = {}
            if self.options.api_key:
                headers["Authorization"] = f"Bearer {self.options.api_key}"
            self._local.client = httpx.Client(
                timeout=self.options.timeout,
                limits=limits,
                headers=headers,
                trust_env=False,
            )
        return self._local.client

    def compatible_recognise_crop(self: Any, image: Any) -> str:
        # GLM-OCR's fixed-prompt chat template expects the image before the
        # instruction. docling-glm-ocr 0.5.0 sends them in the opposite order;
        # vLLM accepts that payload but can silently omit visible text.
        from docling_glm_ocr import model as plugin_model

        payload = {
            "model": self.options.model_name,
            "max_tokens": self.options.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": plugin_model._pil_to_base64_uri(image)},  # noqa: SLF001
                        },
                        {"type": "text", "text": self.options.prompt},
                    ],
                }
            ],
        }
        response = self._get_client().post(self.options.api_url, json=payload)
        if response.status_code >= 400:
            plugin_model.logger.error(
                "vLLM returned HTTP %d for %s: %s",
                response.status_code,
                self.options.api_url,
                response.text,
            )
        response.raise_for_status()
        choices = response.json().get("choices", [])
        if not choices:
            return ""
        return str(choices[0].get("message", {}).get("content", ""))

    GlmOcrRemoteModel.__call__ = compatible_call  # type: ignore[method-assign]
    GlmOcrRemoteModel.post_process_cells = compatible_post_process  # type: ignore[method-assign]
    GlmOcrRemoteModel._get_client = compatible_get_client  # type: ignore[method-assign]
    GlmOcrRemoteModel._recognise_crop = compatible_recognise_crop  # type: ignore[method-assign]
    GlmOcrRemoteModel._mosaicparse_compat = True  # type: ignore[attr-defined]


class GlmOcrRemoteAdapter:
    name = "glm-ocr-remote"

    def __init__(
        self, settings: object | None, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.enabled = bool(setting(settings, "glm_ocr_enabled", True))
        self.api_url = str(
            setting(settings, "glm_ocr_api_url", "http://localhost:8001/v1/chat/completions")
        )
        self.api_key = setting(settings, "glm_ocr_api_key", None)
        self.model = str(setting(settings, "glm_ocr_model", "zai-org/GLM-OCR"))
        self.health_ttl = float(setting(settings, "backend_health_ttl_seconds", 15.0))
        probe_timeout = min(5.0, float(setting(settings, "glm_ocr_timeout_seconds", 120.0)))
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(probe_timeout), trust_env=False
        )
        self._last_status: BackendStatus | None = None
        self._last_probe_monotonic = 0.0

    def _models_url(self) -> str:
        parsed = urlsplit(self.api_url)
        path = parsed.path
        marker = path.find("/v1/")
        base_path = f"{path[:marker]}/v1/models" if marker >= 0 else "/v1/models"
        return urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))

    async def probe(self, *, force: bool = False) -> BackendStatus:
        if not self.enabled:
            return BackendStatus(
                name=self.name,
                state=BackendState.DISABLED,
                enabled=False,
                detail="GLM-OCR is disabled by configuration",
                model=self.model,
            )
        now = time.monotonic()
        if (
            not force
            and self._last_status is not None
            and now - self._last_probe_monotonic <= self.health_ttl
        ):
            return self._last_status
        headers: dict[str, str] = (
            {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        )
        started = time.perf_counter()
        try:
            response = await self._client.get(self._models_url(), headers=headers)
            response.raise_for_status()
            payload = response.json()
            model_items = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(model_items, list):
                raise ValueError("models response has no data list")
            identifiers = {
                item.get("id")
                for item in model_items
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            model_missing = self.model not in identifiers
            detail = (
                "backend is reachable; configured model was not listed"
                if model_missing
                else "OpenAI-compatible models endpoint responded"
            )
            status = BackendStatus(
                name=self.name,
                state=BackendState.UNAVAILABLE if model_missing else BackendState.READY,
                detail=detail,
                model=self.model,
                latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            status = BackendStatus(
                name=self.name,
                state=BackendState.UNAVAILABLE,
                detail=f"health probe failed: {type(exc).__name__}",
                model=self.model,
                latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        self._last_status = status
        self._last_probe_monotonic = now
        return status

    def build_options(
        self,
        *,
        languages: list[str],
        scale: float | None = None,
        force_full_page_ocr: bool = False,
    ) -> GlmOcrRemoteOptions:
        """Build the third-party model only at converter initialization time."""

        _install_plugin_compatibility()
        from docling.datamodel.pipeline_options import OcrMode
        from docling_glm_ocr import GlmOcrRemoteOptions

        options = GlmOcrRemoteOptions(
            api_url=self.api_url,
            api_key=self.api_key,
            model_name=self.model,
            lang=languages,
            scale=float(
                scale if scale is not None else setting(self.settings, "glm_ocr_scale", 3.0)
            ),
            max_image_pixels=int(setting(self.settings, "glm_ocr_max_image_pixels", 4_500_000)),
            max_concurrent_requests=int(setting(self.settings, "glm_ocr_max_concurrency", 4)),
            max_tokens=int(setting(self.settings, "glm_ocr_max_tokens", 4_096)),
            prompt=str(setting(self.settings, "glm_ocr_prompt", "Text Recognition:")),
            timeout=float(setting(self.settings, "glm_ocr_timeout_seconds", 120.0)),
            max_retries=int(setting(self.settings, "glm_ocr_max_retries", 3)),
            mode=OcrMode.FULL_PAGE if force_full_page_ocr else OcrMode.DEFAULT,
        )
        return options

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
