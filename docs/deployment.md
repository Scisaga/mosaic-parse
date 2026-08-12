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

This is the default and works for digital-native PDFs without GLM:

```bash
docker compose up -d --build parser
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

## Mode 3: Remote GLM and/or existing Ollama

Do not enable the bundled profile. Point the parser at services reachable from
its container:

```dotenv
GLM_OCR_ENABLED=1
GLM_OCR_API_URL=http://gpu-host.example:8000/v1/chat/completions
GLM_OCR_MODEL=zai-org/GLM-OCR

VLM_ENABLED=1
VLM_BASE_URL=http://ollama-host.example:11434/v1
VLM_MODEL=qwen3.6:35b
VLM_MAX_RETRIES=1
```

```bash
docker compose up -d --build parser
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

Compose uses named volumes `parser-data`, `docling-models`, and
`huggingface-cache`. Back up `parser-data` before upgrades. Do not use
`docker compose down -v` unless deleting jobs, results, and model caches is
intentional.

Expired jobs can be removed through the protected admin API:

```bash
ADMIN_TOKEN='replace-me' uv run python scripts/cleanup_jobs.py
```

Use `/health` for liveness. Use `/ready` for traffic admission; readiness may be
false while storage or a backend required by the active default route is not
usable. `/v1/backends` reports individual backend capability.

## MCP reverse-proxy allowlist

MCP 2.x validates the request Host and browser Origin to mitigate DNS rebinding.
Defaults allow only localhost and Compose-internal parser names. When exposing
`/mcp` through a domain, add its exact `host[:port]` to `MCP_ALLOWED_HOSTS` and
the exact scheme/host/port Origin to `MCP_ALLOWED_ORIGINS`, for example:

```dotenv
MCP_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],parser,docling_glm_parser,docs.example.com
MCP_ALLOWED_ORIGINS=http://localhost,http://127.0.0.1,https://docs.example.com
```

Do not use `*` as the default. Preserve and validate the original Host header in
the reverse proxy configuration.
