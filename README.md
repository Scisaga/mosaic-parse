<p align="center">
  <img src="logo.png" alt="Docling GLM logo" width="112" />
</p>

# Docling GLM

自托管的文档转 Markdown / Plain Text 微服务。

Docling GLM 使用 Docling 解析数字原生 PDF，并可通过远程 GLM-OCR
识别扫描页和图片区域；对于特殊复杂版式，可选接入兼容 OpenAI API 的
Ollama Vision 模型。项目提供 FastAPI、React Web UI、同步/异步任务、
SSE、MCP、Docker Compose 和 GHCR 镜像。

> Docling GLM is an independent community project and is not affiliated with
> or endorsed by the Docling or GLM-OCR projects. Docling 与 GLM-OCR 名称及
> 商标归各自权利人所有。

## 项目边界

本项目只把 PDF/图片解析为 Markdown 或纯文本。它不实现财务指标抽取、
实体/关系/事件抽取、报告总结、RAG 切片、Embedding、向量数据库、文档
问答、知识图谱、模型训练或长期文档资产管理。上述能力应作为下游项目
消费本服务的文本结果，不能反向侵入解析层。

## 功能

- PDF、PNG、JPEG、WEBP、TIFF 上传和受控 HTTP/HTTPS URL；
- `auto`、`standard`、`ocr`、可选 `vlm` 模式；
- `fast`、`balanced`、`accurate` 质量配置；
- Markdown / Plain Text，支持页码范围与结果下载；
- 小文件同步解析，异步 Job、持久化状态和 SSE 进度；
- `/health`、`/ready`、后端能力探测、Swagger、ReDoc；
- MCP 2.x Streamable HTTP；
- API Key、独立 Admin Token、上传校验和 SSRF 防护；
- CPU-only 主镜像，以及 GPU 模型服务分离部署。

## 快速开始：CPU-only

默认配置不启动也不要求 GLM，数字原生 PDF 可独立解析：

```bash
cp .env.example .env
docker compose up -d --build parser
docker compose ps
```

打开：

- Web UI: <http://localhost:12303/>
- Swagger: <http://localhost:12303/docs>
- ReDoc: <http://localhost:12303/redoc>
- Health: <http://localhost:12303/health>
- MCP: <http://localhost:12303/mcp>

首次转换会下载 Docling 布局/表格模型，`/ready` 在初始化期间可能暂时返回
非 2xx。parser 健康检查为首次下载/初始化保留 10 分钟 `start_period`。
模型保存在 Compose 命名卷中，重启不会重复下载。

容器使用 `DOCLING_LOCAL_ARTIFACTS_PATH` 作为本服务的离线目录配置。不要在
空目录上设置上游同名变量 `DOCLING_ARTIFACTS_PATH`，否则 Docling 会将其视为
已准备好的离线目录并关闭自动下载。

同步解析一个自制测试 PDF：

```bash
curl --fail-with-body http://localhost:12303/v1/documents/parse \
  -F file=@tests/fixtures/native-report.pdf \
  -F mode=standard \
  -F profile=balanced \
  -F output_format=markdown \
  -F language=zh,en
```

## 三种部署模式

| 模式 | 主服务 | OCR/VLM | 启动方式 |
|---|---|---|---|
| CPU-only | 本项目 CPU 镜像 | 关闭；适合数字 PDF | `docker compose up -d --build parser` |
| 本机 GLM | CPU parser | profile 中独立 vLLM，默认 GPU 1 | `docker compose --profile glm up -d --build` |
| 远程后端 | CPU parser | 远程 GLM 和/或既有 Ollama | 配置 URL 后只启动 `parser` |

### 本机 GLM-OCR（GPU 1）

编辑 `.env`：

```dotenv
GLM_OCR_ENABLED=1
GLM_GPU_DEVICE_ID=1
GLM_DTYPE=half
```

然后：

```bash
docker compose --profile glm up -d --build
docker compose logs -f glm-ocr
curl --fail http://localhost:8001/health
```

Compose 将 GPU `device_ids` 默认锁到 `1`。参考主机的 GPU 1 是 RTX 2080 Ti
（Turing，不支持 BF16），因此 vLLM 默认使用 FP16 `--dtype half`。主 parser
仍不挂载 GPU，也不包含 CUDA 依赖。

### 远程 GLM / Ollama

```dotenv
GLM_OCR_ENABLED=1
GLM_OCR_API_URL=http://gpu-host:8000/v1/chat/completions
GLM_OCR_MODEL=zai-org/GLM-OCR

VLM_ENABLED=1
VLM_BASE_URL=http://ollama-host:11434/v1
VLM_MODEL=qwen3.6:27b
VLM_DIAGRAM_ENRICHMENT_ENABLED=1
```

`auto` 会优先用 GLM 全页 OCR 修复可明确识别的异常 PDF 字符映射；检测到带
“流程图 / flowchart / diagram”等窄范围图注的 PictureItem 时，可把原图裁剪
交给 VLM 生成经严格校验的 Mermaid，并在保留 `<!-- image -->` 占位的同时紧邻
插入代码块。该 Mermaid 是模型推断结果，可能误判节点或连线，使用前必须人工
对照原文复核；可用 `VLM_DIAGRAM_ENRICHMENT_ENABLED=0` 独立关闭出站调用与成本。

```bash
docker compose up -d --build parser
curl --fail http://localhost:12303/v1/backends
```

完整部署、显卡校验、离线模型与反向代理说明见
[docs/deployment.md](docs/deployment.md)。

## API 概览

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/documents/parse` | 小文档同步解析 |
| `POST` | `/v1/documents/jobs` | 创建异步任务 |
| `GET` | `/v1/documents/jobs/{job_id}` | 查询持久化状态 |
| `GET` | `/v1/documents/jobs/{job_id}/events` | SSE 进度 |
| `GET` | `/v1/documents/jobs/{job_id}/result` | 获取/下载 Markdown 或 Text |
| `POST` | `/v1/documents/jobs/{job_id}/retry` | 重试失败任务 |
| `DELETE` | `/v1/documents/jobs/{job_id}` | 取消或删除任务 |
| `GET` | `/v1/backends` | 后端真实能力状态 |
| `GET` | `/health`, `/ready` | 存活与就绪检查 |
| `GET/POST` | `/mcp` | MCP Streamable HTTP |
| `POST` | `/admin/reload`, `/admin/cleanup` | 管理操作 |

请求参数、状态机、SSE 和错误协议见 [docs/api.md](docs/api.md)，在线 Schema
以 `/openapi.json` 为准。

若设置 `API_KEY`，公共受保护接口推荐使用：

```http
Authorization: Bearer <API_KEY>
```

`X-API-Key` 也可用作客户端兼容别名。管理接口始终使用独立的：

```http
X-Admin-Token: <ADMIN_TOKEN>
```

不要复用两个密钥，也不要把密钥放进 URL、Dockerfile 或日志。

## MCP

MCP 使用 Python SDK 2.x 的 Streamable HTTP 传输，暴露解析、创建任务、查询
任务和获取结果工具。大文件使用受控 `source_url`；MCP 不接受任意本地文件
路径。

MCP v2 会校验 Host 和 Origin 防止 DNS rebinding。通过公网域名/反向代理
暴露时，必须把精确 `host[:port]` 和 Origin 添加到：

```dotenv
MCP_ALLOWED_HOSTS=localhost,127.0.0.1,[::1],parser,docling_glm_parser,docs.example.com
MCP_ALLOWED_ORIGINS=http://localhost,http://127.0.0.1,https://docs.example.com
```

默认不使用通配符。

## 本地开发

需要 Python `>=3.13,<3.14`、uv、Node 20：

```bash
uv sync --frozen
npm --prefix frontend ci
npm --prefix frontend run build
DATA_DIR=./data STATIC_DIR=frontend/dist GLM_OCR_ENABLED=0 \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 12303
```

依赖基线经过 Python 3.13 解算并提交在 `uv.lock`：

| 组件 | 锁定版本 |
|---|---:|
| Docling | `2.119.0` |
| docling-glm-ocr | `0.5.0` |
| FastAPI | `0.141.1` |
| MCP Python SDK | `2.0.0` |
| PyTorch CPU | `2.13.0+cpu` |
| vLLM GLM image | `v0.19.1` |

主服务的 PyTorch/torchvision 从官方 CPU wheel 索引锁定；`uv.lock` 不含
`nvidia-*` 或 CUDA Python 包。

CPU 基线使用 `DOCLING_COMPILE_MODELS=0`，避免首次文档触发耗时很长的
`torch.compile`；只应在固定硬件上完成预热与基准验证后开启。远程 VLM
默认 `VLM_MAX_RETRIES=1`，防止超时/过载时形成长时间重试风暴。

## 验证

```bash
uv lock --check
uv sync --frozen
uv run ruff check .
uv run mypy app
uv run pytest --cov=app
uv run python scripts/export_openapi.py --check
uv run python scripts/generate_fixtures.py --check

npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run test
npm --prefix frontend run build

docker compose config --quiet
docker build -t docling-glm-parser:local .
```

CI 不下载或运行大模型；GLM/Ollama 错误路径由 mock 服务覆盖。真实 GPU
回归应在专用环境执行。夹具全部由本项目脚本生成，不包含第三方文档原文，
详见 [tests/fixtures/README.md](tests/fixtures/README.md)。

## 设计与许可证

- [架构](docs/architecture.md)
- [API](docs/api.md)
- [部署](docs/deployment.md)
- [基准测试](docs/benchmark.md)
- [项目 v0.1 设计规格](docs/project-spec.md)

本项目代码使用 [Apache License 2.0](LICENSE)。Docling、GLM-OCR、模型权重
和容器镜像拥有各自许可证；部署者需要分别核查并遵守其版本对应条款。
