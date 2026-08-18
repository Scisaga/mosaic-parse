"""Remote Ollama/OpenAI-compatible structured visual adapter."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from app.models.backend import BackendState, BackendStatus
from app.models.source import StoredSource
from app.parsers.base import (
    ParserError,
)
from app.utils.settings import setting

StructuredResultT = TypeVar("StructuredResultT", bound=BaseModel)


class VlmResponseTruncatedError(ParserError):
    """The model consumed its output budget before producing a final answer."""


class VlmEmptyContentError(ParserError):
    """The model returned no final content after optional reasoning."""


class VlmSchemaError(ParserError):
    """The final content did not satisfy the requested schema."""


@dataclass(frozen=True, slots=True)
class StructuredVlmCompletion[StructuredResultT: BaseModel]:
    value: StructuredResultT
    duration_ms: int
    finish_reason: str
    reasoning_characters: int
    prompt_tokens: int | None
    completion_tokens: int | None


class OllamaVisualAdapter:
    name = "ollama-vlm"

    def __init__(
        self, settings: object | None, http_client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.enabled = bool(setting(settings, "vlm_enabled", False))
        self.base_url = str(setting(settings, "vlm_base_url", "http://localhost:11434/v1")).rstrip(
            "/"
        )
        self.api_key = setting(settings, "vlm_api_key", "ollama")
        self.model = str(setting(settings, "vlm_model", "qwen3.6:35b"))
        self.timeout = float(setting(settings, "vlm_timeout_seconds", 300.0))
        self.max_retries = int(setting(settings, "vlm_max_retries", 1))
        self.temperature = float(setting(settings, "vlm_temperature", 0.0))
        self.health_ttl = float(setting(settings, "backend_health_ttl_seconds", 15.0))
        self._semaphore = asyncio.Semaphore(int(setting(settings, "vlm_max_concurrency", 1)))
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), trust_env=False
        )
        self._last_status: BackendStatus | None = None
        self._last_probe = 0.0

    async def initialize(self) -> None:
        if self.enabled:
            await self.probe(force=True)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def probe(self, *, force: bool = False) -> BackendStatus:
        if not self.enabled:
            return BackendStatus(
                name=self.name,
                state=BackendState.DISABLED,
                enabled=False,
                detail="VLM is disabled by configuration",
                model=self.model,
            )
        now = time.monotonic()
        if not force and self._last_status and now - self._last_probe <= self.health_ttl:
            return self._last_status
        started = time.perf_counter()
        try:
            response = await self._client.get(
                f"{self.base_url}/models",
                headers=self._headers,
                timeout=min(5.0, self.timeout),
            )
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
            if self.model not in identifiers:
                state = BackendState.UNAVAILABLE
                detail = "Ollama is reachable but the configured VLM is not installed"
            else:
                state = BackendState.READY
                detail = "OpenAI-compatible models endpoint responded"
            status = BackendStatus(
                name=self.name,
                state=state,
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
        self._last_probe = now
        return status

    @staticmethod
    def _render_pdf_page(path: Path, page_number: int, scale: float) -> bytes:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # type: ignore[no-redef]
        with pymupdf.open(path) as document:
            page = document.load_page(page_number - 1)
            matrix = pymupdf.Matrix(scale, scale)
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            return pixmap.tobytes("png")

    @staticmethod
    def _render_image_page(path: Path, page_number: int) -> bytes:
        from PIL import Image

        with Image.open(path) as image:
            image.seek(page_number - 1)
            frame = image.convert("RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            return buffer.getvalue()

    async def _render(self, source: StoredSource, page_number: int, profile: str) -> bytes:
        scale = 1.5 if profile == "fast" else (2.5 if profile == "accurate" else 2.0)
        if source.mime_type == "application/pdf":
            return await asyncio.to_thread(self._render_pdf_page, source.path, page_number, scale)
        return await asyncio.to_thread(self._render_image_page, source.path, page_number)

    async def _complete_images(
        self,
        images: list[bytes],
        prompt: str,
        *,
        max_tokens: int | None = None,
        reasoning_effort: str = "none",
        response_schema: type[StructuredResultT] | None = None,
    ) -> tuple[str, dict[str, int | str | None]]:
        if not images:
            raise ParserError("Ollama VLM request contains no image")
        content_parts: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        content_parts.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}"
                },
            }
            for image in images
        )
        payload: dict[str, object] = {
            "model": self.model,
            "temperature": self.temperature,
            "reasoning_effort": reasoning_effort,
            "messages": [
                {
                    "role": "user",
                    "content": content_parts,
                }
            ],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_schema is not None:
            schema = response_schema.model_json_schema()
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": schema,
                },
            }
            payload["messages"][0]["content"][0]["text"] = (  # type: ignore[index]
                f"{prompt}\nReturn JSON matching this schema exactly: "
                f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
            )
        data: dict[str, object] | None = None
        started = time.perf_counter()
        async with self._semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await self._client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    decoded = response.json()
                    if not isinstance(decoded, dict):
                        raise ValueError("response root is not an object")
                    data = decoded
                    break
                except httpx.HTTPStatusError as exc:
                    # Authentication, validation, and payload errors are
                    # deterministic and must not be retried.
                    if exc.response.status_code < 500 or attempt >= self.max_retries:
                        raise ParserError(
                            f"Ollama VLM returned HTTP {exc.response.status_code}"
                        ) from exc
                except httpx.TimeoutException as exc:
                    if attempt >= self.max_retries:
                        raise ParserError("Ollama VLM request timed out") from exc
                except httpx.HTTPError as exc:
                    if attempt >= self.max_retries:
                        raise ParserError(
                            f"Ollama VLM request failed: {type(exc).__name__}"
                        ) from exc
                except ValueError as exc:
                    raise ParserError("Ollama VLM returned invalid JSON") from exc
                await asyncio.sleep(min(2**attempt, 4))
        if data is None:
            raise ParserError("Ollama VLM returned no response")
        try:
            choices = data["choices"]
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise TypeError
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content", "")
            finish_reason = choices[0].get("finish_reason")
            reasoning = message.get("reasoning", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ParserError("Ollama VLM returned an invalid response") from exc
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        if not isinstance(content, str):
            raise ParserError("Ollama VLM returned non-text content")
        finish_reason = finish_reason if isinstance(finish_reason, str) else "unknown"
        if finish_reason == "length":
            raise VlmResponseTruncatedError("Ollama VLM exhausted its output-token budget")
        content = content.strip()
        if not content:
            raise VlmEmptyContentError("Ollama VLM returned no final content")
        usage = data.get("usage")
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        if isinstance(usage, dict):
            raw_prompt = usage.get("prompt_tokens")
            raw_completion = usage.get("completion_tokens")
            prompt_tokens = raw_prompt if isinstance(raw_prompt, int) and raw_prompt >= 0 else None
            completion_tokens = (
                raw_completion if isinstance(raw_completion, int) and raw_completion >= 0 else None
            )
        metadata: dict[str, int | str | None] = {
            "duration_ms": max(0, round((time.perf_counter() - started) * 1_000)),
            "finish_reason": finish_reason,
            "reasoning_characters": len(reasoning) if isinstance(reasoning, str) else 0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        return content, metadata

    async def complete_structured(
        self,
        images: list[bytes],
        prompt: str,
        schema: type[StructuredResultT],
        *,
        max_tokens: int,
        reasoning_effort: str,
    ) -> StructuredVlmCompletion[StructuredResultT]:
        content, metadata = await self._complete_images(
            images,
            prompt,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            response_schema=schema,
        )
        try:
            value = schema.model_validate_json(content)
        except ValidationError as exc:
            raise VlmSchemaError("Ollama VLM response failed schema validation") from exc
        return StructuredVlmCompletion(
            value=value,
            duration_ms=int(metadata["duration_ms"] or 0),
            finish_reason=str(metadata["finish_reason"] or "unknown"),
            reasoning_characters=int(metadata["reasoning_characters"] or 0),
            prompt_tokens=cast(int | None, metadata["prompt_tokens"]),
            completion_tokens=cast(int | None, metadata["completion_tokens"]),
        )

    @staticmethod
    def _rotate_image(image: bytes, degrees: int | None) -> bytes:
        if degrees not in {90, 180, 270}:
            return image
        from PIL import Image

        with Image.open(io.BytesIO(image)) as source:
            rotated = source.convert("RGB").rotate(-degrees, expand=True)
            buffer = io.BytesIO()
            rotated.save(buffer, format="PNG")
            return buffer.getvalue()

    @staticmethod
    def _crop_normalized_image(
        image: bytes,
        normalized_bbox: tuple[float, float, float, float],
    ) -> bytes:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as source:
            frame = source.convert("RGB")
            left, top, right, bottom = normalized_bbox
            pad_x = min(0.01, left, 1 - right)
            pad_y = min(0.01, top, 1 - bottom)
            crop = frame.crop(
                (
                    round((left - pad_x) * frame.width),
                    round((top - pad_y) * frame.height),
                    round((right + pad_x) * frame.width),
                    round((bottom + pad_y) * frame.height),
                )
            )
            if crop.width < 2 or crop.height < 2:
                raise ValueError("table crop is empty")
            buffer = io.BytesIO()
            crop.save(buffer, format="PNG")
            return buffer.getvalue()

    @staticmethod
    def _rotate_bbox(
        bbox: tuple[float, float, float, float],
        degrees: int | None,
    ) -> tuple[float, float, float, float]:
        left, top, right, bottom = bbox
        if degrees == 90:
            return 1 - bottom, left, 1 - top, right
        if degrees == 180:
            return 1 - right, 1 - bottom, 1 - left, 1 - top
        if degrees == 270:
            return top, 1 - right, bottom, 1 - left
        return bbox

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
