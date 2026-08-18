# syntax=docker/dockerfile:1.7

FROM node:20-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.13 AS uv-bin

FROM python:3.13-slim-bookworm AS python-deps
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY --from=uv-bin /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim-bookworm AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    HOST=0.0.0.0 \
    PORT=12303 \
    PATH=/app/.venv/bin:$PATH \
    VIRTUAL_ENV=/app/.venv \
    DATA_DIR=/data \
    STATIC_DIR=/app/static \
    DOCLING_DEVICE=cpu \
    DOCLING_COMPILE_MODELS=0 \
    DOCLING_LOCAL_ARTIFACTS_PATH=/models/docling \
    HF_HOME=/models/huggingface

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
       curl \
       ffmpeg \
       libgl1 \
       libglib2.0-0 \
       libgomp1 \
       tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home app \
    && mkdir -p /data /models/docling /models/huggingface /app/static \
    && chown -R app:app /data /models /app

COPY --from=python-deps --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app/ ./app/
COPY --from=frontend-builder --chown=app:app /build/frontend/dist/ ./static/
COPY --chown=app:app README.md LICENSE ./

USER app
EXPOSE 12303
VOLUME ["/data", "/models/docling", "/models/huggingface"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10m --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-12303}/health" || exit 1
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-12303}\""]
