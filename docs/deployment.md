# Deployment

The parser listens on port `12303`. Its image is CPU-only and uses Python 3.13
CPU wheels for PyTorch. Model inference is isolated in other processes.

## Prerequisites

- Docker Engine with Compose v2 (the current Compose specification is used).
- At least 8 GiB RAM for normal Docling conversion; complex/large PDFs may need
  more.
- Internet access on first use, or prefetched Docling artifacts.
- NVIDIA Container Toolkit only for the optional `glm` profile.

Copy configuration before changing defaults:

```bash
cp .env.example .env
```

Never place production secrets in a committed `.env` file.

On networks that require an outbound proxy, set `HTTP_PROXY` and
`HTTPS_PROXY`; Compose passes them to the parser and GLM model containers for
first-run model downloads. Keep internal service names in `NO_PROXY`. Document
source downloads and GLM/Ollama API clients deliberately use `trust_env=False`,
so an ambient proxy cannot bypass their SSRF/backend routing controls.

## Mode 1: CPU parser only

This is the default and works for digital-native PDF/Office content without GLM.
Purely visual images and standalone videos still require an enabled VLM:

```bash
docker compose up -d --build mosaicparse
docker compose ps
curl --fail http://127.0.0.1:12303/health
curl --fail http://127.0.0.1:12303/ready
```

Keep `GLM_OCR_ENABLED=0` and `VLM_ENABLED=0`. The first conversion may download
Docling layout/table models to the persistent `docling-models` volume. Parser
health checks have a ten-minute startup grace period for that download and CPU
initialization. `DOCLING_COMPILE_MODELS=0` avoids a very slow first document
caused by `torch.compile`; enable it only after workload-specific benchmarking
and a controlled warm-up.

The service uses `DOCLING_LOCAL_ARTIFACTS_PATH=/models/docling` so its setting
does not collide with Docling's own `DOCLING_ARTIFACTS_PATH` environment
variable. Do not set the upstream variable to a fresh empty volume: upstream
interprets it as an offline artifact directory and disables automatic download.

The main image includes FFmpeg and FFprobe. Standalone video defaults are 200 MiB,
30 minutes, at most 24 keyframes, 7680×4320 source frames, two FFmpeg threads and
one concurrent FFmpeg process. Tune `VIDEO_MAX_FRAME_PIXELS`, `FFMPEG_THREADS`,
`FFMPEG_MAX_CONCURRENCY`, and `FFMPEG_TIMEOUT_SECONDS` only with matching container
CPU/memory limits and workload tests.

For offline hosts, prefetch on a connected machine and transfer the artifacts:

```bash
uv run python scripts/download_docling_models.py --output-dir models/docling
```

When using a bind mount instead of the named volume, mount that directory at
`/models/docling` and set `DOCLING_MODEL_DOWNLOAD=0` after verifying it.

## Mode 2: Parser plus bundled GLM-OCR on GPU 1

The repository pins the model server to `vllm/vllm-openai:v0.19.1`.
This patch release includes the Transformers 5 support required by GLM-OCR,
and its standard x86_64 image retains the `sm_75` kernels needed by RTX 20xx
GPUs. The `v0.19.0-ubuntu2404` variant omits `sm_75` and is therefore not a
safe default for the reference RTX 2080 Ti. `.env.example` defaults
`GLM_GPU_DEVICE_ID=1`.

Set:

```dotenv
GLM_OCR_ENABLED=1
GLM_GPU_DEVICE_ID=1
GLM_DTYPE=half
GLM_OCR_MAX_TOKENS=4096
GLM_OCR_PROMPT=Text Recognition:
```

Then start the optional profile:

```bash
docker compose --profile glm up -d --build
docker compose --profile glm ps
docker compose logs -f glm-ocr
```

The host binds GLM to `127.0.0.1:8001` by default, while the parser calls the
Compose-internal URL `http://glm-ocr:8000/v1/chat/completions`. GPU reservation
is explicit; the two GPUs are not treated as a shared memory pool. Set
`GLM_OCR_BIND_ADDRESS` deliberately if another host must reach the model API;
do not expose an unauthenticated vLLM endpoint to an untrusted network.

Before production use, verify the selected GPU and memory allocation:

```bash
nvidia-smi --query-gpu=index,name,memory.total,memory.used --format=csv
docker inspect docling_glm_ocr --format '{{json .HostConfig.DeviceRequests}}'
curl --fail http://127.0.0.1:8001/health
```

The Compose default `GLM_GPU_MEMORY_UTILIZATION=0.40` was validated for a
single-concurrency workload on the 22 GiB development GPU. Increase it only
when higher concurrent context capacity is required and the GPU is dedicated to
this service. GPU 1 in the reference host is an RTX 2080 Ti (Turing), which
cannot execute BF16; the Compose baseline therefore passes
`--dtype half` for FP16. Change `GLM_DTYPE` only after verifying the target GPU.
`GLM_OCR_MAX_TOKENS` is the response budget and must remain below
`GLM_MAX_MODEL_LEN` so the image and prompt still fit in the context window.
The default `GLM_OCR_PROMPT=Text Recognition:` follows GLM-OCR's fixed prompt
contract; arbitrary prose prompts can silently omit otherwise visible text.
Model-server startup includes a potentially large Hugging Face download and has
a five-minute health-check grace period.

## Mode 3: Bundled official GLM-OCR SDK page pipeline

The `glm-sdk` profile starts the same GPU model service plus a CPU-only SDK
sidecar for PP-DocLayoutV3 and region orchestration:

```dotenv
GLM_OCR_ENABLED=1
GLM_SDK_ENABLED=1
VISUAL_ROUTER_ENABLED=1
VLM_ENABLED=1
VLM_MODEL=qwen3.6-docparse:35b-32k
```

```bash
docker compose --profile glm-sdk up -d --build
docker compose --profile glm-sdk ps
docker compose logs -f glm-ocr-sdk
```

`profile=accurate` sends measured complex visual regions to the SDK and
Qwen. Healthy native and sparse pages stay on Docling. The SDK sidecar is
configured with `layout.device=cpu`, `batch_size=1`, and `max_workers=1`; it
reuses `http://glm-ocr:8000` and must not receive a GPU device reservation.
Set `VISUAL_ROUTER_ENABLED=0` for an immediate automatic-route rollback.

## Mode 4: Remote GLM and/or existing Ollama

Do not enable the bundled profile. Point the parser at services reachable from
its container:

```dotenv
GLM_OCR_ENABLED=1
GLM_OCR_API_URL=http://gpu-host.example:8000/v1/chat/completions
GLM_OCR_MODEL=zai-org/GLM-OCR

VLM_ENABLED=1
VLM_BASE_URL=http://ollama-host.example:11434/v1
VLM_MODEL=qwen3.6-docparse:35b-32k
VLM_MAX_RETRIES=1
VLM_MAX_CONCURRENCY=1
VLM_PAGE_BUDGET_SECONDS=180
VLM_MAX_CALLS_PER_PAGE=3
VLM_PLAN_MAX_TOKENS=4096
VLM_REGION_MAX_TOKENS=16384
VLM_CONFLICT_MAX_TOKENS=8192
VLM_REASONING_EFFORT=low
VLM_CONFLICT_REASONING_EFFORT=medium
MEDIA_VLM_REASONING_EFFORT=none
VIDEO_SUMMARY_REASONING_EFFORT=none
```

图片与视频描述默认关闭 reasoning，避免简单结构化描述消耗完整输出预算；文档视觉融合仍使用独立的 reasoning 配置。

Create the fixed 32K Ollama alias on the model host before starting the parser:

```dockerfile
FROM qwen3.6:35b
PARAMETER num_ctx 32768
```

```bash
ollama create qwen3.6-docparse:35b-32k -f Modelfile
curl --fail http://ollama-host.example:11434/api/ps
```

The OpenAI-compatible request cannot change the loaded context length per call;
the alias therefore owns `num_ctx`. Structured visual responses use JSON Schema
through `response_format` and are validated again by Pydantic in the parser.
See [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
and [Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs).

```bash
docker compose up -d --build mosaicparse
curl --fail http://127.0.0.1:12303/v1/backends
```

Container DNS must resolve those hostnames. On Linux, `host.docker.internal` may
require an explicit `extra_hosts` entry. Use TLS and protected network paths
when backends cross a trust boundary.

## Source checkout without Docker

```bash
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
DATA_DIR=./data STATIC_DIR=frontend/dist GLM_OCR_ENABLED=0 \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 12303
```

Python must be `>=3.13,<3.14`. The committed lock uses official CPU PyTorch
wheels; it does not install CUDA libraries into the parser environment.

## Data, upgrades, and cleanup

Compose uses named volumes `mosaicparse-data`, `docling-models`, and
`huggingface-cache`. Do not use `docker compose down -v` unless deleting jobs,
results, and model caches is intentional.

0.4.0 intentionally does not convert older Job results. For the breaking upgrade,
stop the main service and run the guarded purge command with the exact confirmation
string. It deletes Job rows, inputs, results, media, keyframes, bundles and legacy
`jobs.sqlite3`, while preserving the current `jobs.db` schema and model volumes:

```bash
docker compose stop mosaicparse
docker run --rm --network mosaicparse_default \
  -v mosaicparse-data:/data \
  ghcr.io/scisaga/mosaic-parse:0.4.0 \
  python /app/scripts/purge_job_data.py \
  --health-url http://mosaicparse:12303/health \
  --data-dir /data \
  --confirm PURGE_MOSAICPARSE_JOBS_0_4_0
```

The command rejects a running service, root/relative/symlink targets and an incorrect
confirmation string, reports deletion counts, and can be rerun idempotently. This
upgrade has no Job backup or data rollback; back up the data volume yourself before
running it if that is required by your deployment policy.

Expired jobs can be removed through the protected admin API:

```bash
ADMIN_TOKEN='replace-me' uv run python scripts/cleanup_jobs.py
```

Use `/health` for liveness. Use `/ready` for traffic admission; readiness may be
false while storage or a backend required by the active default route is not
usable. `/v1/backends` reports individual backend capability.

## MCP reverse-proxy allowlist

MCP 2.x validates the request Host and browser Origin to mitigate DNS rebinding.
Defaults allow only localhost and the Compose-internal MosaicParse name. When exposing
`/mcp` through a domain, add its exact `host[:port]` to `MCP_ALLOWED_HOSTS` and
the exact scheme/host/port Origin to `MCP_ALLOWED_ORIGINS`, for example:

```dotenv
MCP_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],mosaicparse,docs.example.com
MCP_ALLOWED_ORIGINS=http://localhost,http://127.0.0.1,https://docs.example.com
```

Do not use `*` as the default. Preserve and validate the original Host header in
the reverse proxy configuration.
