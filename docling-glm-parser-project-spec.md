# Docling GLM 项目初始化与架构设计文档

> 文档版本：v0.1 设计稿  
> 项目显示名称：**Docling GLM**  
> 推荐仓库名：**docling-glm-parser**  
> 推荐 Python 包名：**docling_glm_parser**  
> 推荐镜像名：**ghcr.io/scisaga/docling-glm-parser**  
> 默认服务端口：**12303**

---

## 1. 项目命名结论

`docling-glm` 作为产品显示名称是合适的，简短、直接，能表达“Docling 文档解析 + GLM-OCR 识别”的核心组合。

但不建议将公开仓库直接命名为 `docling-glm`，主要有三个原因：

1. 已存在名为 `docling-glm-ocr` 的第三方 Docling OCR 插件，仓库名过近，容易让使用者误认为两者是同一项目或上下游官方项目。
2. `docling-glm` 容易被理解为 Docling 官方维护的 GLM 集成项目。
3. 本项目后续可能同时接入 Ollama/Qwen VLM、其他 OCR 后端和任务系统，`docling-glm` 对项目范围描述略窄。

推荐命名如下：

| 类型 | 建议名称 |
|---|---|
| 产品/UI 标题 | `Docling GLM` |
| GitHub 仓库 | `docling-glm-parser` |
| Python 包 | `docling_glm_parser` |
| Docker 服务 | `docling_glm_parser` |
| Docker 镜像 | `ghcr.io/scisaga/docling-glm-parser` |
| API 标识 | `docling-glm-parser` |

README 首页应增加声明：

> Docling GLM is an independent community project and is not affiliated with or endorsed by the Docling or GLM-OCR projects.

若项目只在内部使用，直接使用 `docling-glm` 也没有实质问题；本文后续统一采用推荐仓库名 `docling-glm-parser`。

---

## 2. 项目定位

### 2.1 一句话定义

**Docling GLM 是一个自托管的文档转文本微服务：使用 Docling 解析数字原生文档，使用远程 GLM-OCR 补充扫描件和图片区域识别，并提供自研 API、Web UI、异步任务和 MCP 接口。**

### 2.2 输入

第一阶段支持：

- PDF；
- PNG、JPEG、WEBP、TIFF；
- 文件上传；
- HTTP/HTTPS 文档 URL；
- 可选页码范围。

后续可扩展：

- DOCX；
- PPTX；
- XLSX；
- 批量压缩包。

### 2.3 输出

面向调用方只提供文本类结果：

- Markdown；
- Plain Text。

API 自身使用 JSON 响应封装任务状态、耗时、告警和结果文本，但不把“财务指标 JSON”“实体关系 JSON”等内容纳入本项目。

### 2.4 明确不做

本项目第一阶段不负责：

- 财务指标抽取；
- 实体、关系和事件抽取；
- 研报总结、观点分析；
- RAG 切片、Embedding、向量入库；
- 文档问答；
- 知识图谱；
- 模型训练或微调；
- 多用户文档管理平台；
- 长期文档资产库。

后续指标抽取项目只消费本项目产生的 Markdown/Text，不反向侵入解析服务。

---

## 3. 与现有项目的关系

### 3.1 参考 `qwen3-embedding-openai` 的部分

沿用以下工程思路：

- FastAPI 作为统一外层服务；
- 自研 Web UI，而不是使用上游演示 UI；
- Vite 独立构建前端，由 FastAPI 托管静态产物；
- `/health`、`/ready`、`/docs`、`/redoc`；
- HTTP MCP 挂载到 `/mcp`；
- 管理接口由独立 Admin Token 保护；
- 多阶段 Docker 构建；
- Docker Compose 配置；
- GitHub Actions 自动测试、构建并发布 GHCR 镜像；
- 通过环境变量控制后端地址和运行参数。

### 3.2 不直接复制的部分

`qwen3-embedding-openai` 当前业务相对集中，根目录采用较扁平的 `app.py + service.py` 结构。文档解析涉及上传、异步任务、多个解析后端、缓存、结果导出和页面级状态，新项目不应继续把大量逻辑堆进单个 `app.py`。

新项目采用 Python 包结构，FastAPI 路由、任务服务、解析器、后端适配器、数据模型和 UI 分开组织。

### 3.3 与 Docling Serve 的关系

本项目：

- **使用 `docling` Python SDK**；
- **不依赖 `docling-serve`**；
- 不复用 Docling Serve 的 API；
- 不复用 Docling Serve 的 Gradio UI；
- 不 fork Docling 核心代码；
- 通过稳定的适配层调用 Docling。

因此上游 Docling 升级时，只需要修改 `DoclingStandardParser` 适配层，不会迫使 UI 和公共 API 一起变化。

---

## 4. 总体架构

由于目标环境不一定能正常渲染 Mermaid，本文全部使用文本架构图。

```text
浏览器 / HTTP 客户端 / MCP 客户端
                  │
                  ▼
┌──────────────────────────────────────────────┐
│          Docling GLM FastAPI 服务            │
│                                              │
│  API / 鉴权 / 上传 / URL 获取 / 参数校验      │
│  同步解析 / 异步任务 / SSE 进度 / 结果下载     │
│  Web UI / Swagger / ReDoc / MCP / Health     │
└──────────────────────────────────────────────┘
                  │
                  ▼
┌──────────────────────────────────────────────┐
│              Parser Coordinator              │
│                                              │
│  解析模式选择、Converter 缓存、超时、重试      │
│  结果规范化、Markdown/Text 导出、质量告警      │
└──────────────────────────────────────────────┘
        │                         │
        │ 默认                    │ 可选
        ▼                         ▼
┌──────────────────────┐   ┌──────────────────────┐
│ Docling Standard     │   │ Docling VLM Pipeline │
│ CPU 为主             │   │ Ollama/Qwen Vision   │
│                      │   │ 5090，默认关闭        │
│ 原生 PDF 文字         │   │ 复杂页面人工/自动回退  │
│ Layout / Reading     │   └──────────────────────┘
│ Table Structure      │
│ GLM OCR Plugin       │
└──────────────────────┘
        │
        │ OpenAI-compatible HTTP
        ▼
┌──────────────────────────────┐
│ GLM-OCR 0.9B 推理服务         │
│ RTX 5060 Ti / RTX 2080 Ti    │
│ vLLM 或官方 GLM-OCR 服务      │
└──────────────────────────────┘
        │
        ▼
┌──────────────────────────────┐
│ 统一 DocumentParseResult      │
│ Markdown / Text / 告警 / 耗时  │
└──────────────────────────────┘
```

### 4.1 主服务是否需要 GPU

主 FastAPI 容器不要求 GPU：

- Docling Standard 默认在 CPU 上运行；
- GLM-OCR 通过 HTTP 调用外部 GPU 服务；
- Qwen3.6 通过 HTTP 调用现有 Ollama 服务；
- UI、任务管理和输出规范化全部在 CPU 上完成。

这样可将服务部署为：

```text
CPU 主机 / 容器
└── docling-glm-parser

RTX 5060 Ti 或 RTX 2080 Ti
└── GLM-OCR 推理服务

RTX 5090
└── Ollama + qwen3.6:35b（可选 VLM 回退）
```

### 4.2 为什么第一版不再增加独立 PDF Router

Docling Standard 已经能够：

- 使用 PDF 原生文字层；
- 分析版面与阅读顺序；
- 识别表格结构；
- 只在需要的区域调用 OCR。

第一版无需再用 PyMuPDF 自己实现一套“数字 PDF/扫描 PDF”分流器，否则会形成两套页面判断和两套结果融合逻辑。

第一版的自动模式应当是：

```text
Docling Standard
    ├── 原生文字区域：直接使用 PDF 内容
    └── OCR 区域：调用远程 GLM-OCR
```

只有当标准流程发生空页、严重乱码、重复输出或转换失败时，才进入后续 VLM 回退。

---

## 5. 解析后端设计

### 5.1 后端 A：Docling Standard

定位：默认主路径。

主要职责：

- 原生 PDF 文字读取；
- Layout；
- Reading Order；
- 标题、段落、列表和表格结构；
- 将 OCR 结果合并回统一文档结构；
- 导出 Markdown 和 Plain Text。

建议默认配置：

```text
do_ocr = true
do_table_structure = true
force_backend_text = false
table_mode = accurate
allow_external_plugins = true
document_timeout = 可配置
accelerator_device = cpu
```

`force_backend_text` 不应默认设为 `true`。它适合原生文字层非常可靠、但 Docling 文字检测出现问题的文档；默认强制开启可能削弱布局分析。将其作为高级参数或独立 profile。

### 5.2 后端 B：远程 GLM-OCR 插件

定位：Docling Standard 内部的 OCR Engine。

建议第一版优先使用现有 `docling-glm-ocr` 插件完成验证：

```text
Docling Layout
    │
    ├── 识别需要 OCR 的区域
    ├── 裁剪页面区域
    ├── Base64 编码
    └── 调用远程 GLM-OCR Chat Completions API
            │
            ▼
       返回 Markdown 文本
            │
            ▼
       合并回 DoclingDocument
```

第一版使用第三方插件的理由：

- 已完成 Docling `BaseOcrModel` 接口适配；
- 已支持并行请求、超时、重试、图片缩放和像素上限；
- 与远程 vLLM OpenAI-compatible 接口兼容；
- 可以显著减少初始化项目需要编写的模型适配代码。

但要保留以下限制说明：

- 该插件不是官方 GLM-OCR 完整两阶段 SDK；
- 它使用 Docling 的 Layout 区域，而不是 PP-DocLayout-V3；
- 默认提示词和语言参数需要针对中文文档调整；
- 不应伪造 OCR 置信度；
- 页面级后端追踪应以实际可观测信息为准，不得编造。

建议封装为本项目自己的适配器：

```text
GlmOcrRemoteAdapter
└── 内部依赖 docling-glm-ocr
```

公共业务代码不得直接依赖第三方插件的数据结构。

### 5.3 后端 C：官方 GLM-OCR 完整 Pipeline

定位：第二阶段的扫描件/复杂 OCR fallback。

官方完整 GLM-OCR SDK 包含：

- PageLoader；
- PP-DocLayout-V3；
- GLM-OCR Client；
- 按区域并行识别；
- ResultFormatter；
- Markdown 和布局 JSON 输出。

它与 Docling OCR 插件的区别：

| 对比项 | Docling + GLM 插件 | 官方 GLM-OCR Pipeline |
|---|---|---|
| Layout | Docling Layout | PP-DocLayout-V3 |
| 主文档结构 | DoclingDocument | GLM SDK 结果 |
| OCR 调用粒度 | Docling 识别出的区域 | GLM SDK 识别出的区域 |
| 与 Docling 表格/阅读顺序融合 | 原生 | 需要额外归一化 |
| 第一版接入成本 | 低 | 中等 |
| 适合作为 | 默认 OCR Engine | 扫描/异常文档 fallback |

因此第一版不必把官方完整 SDK 和 Docling 默认串行执行；第二阶段增加 `GlmSdkParser`，在 Docling 结果质量异常时整体回退。

### 5.4 后端 D：Ollama/Qwen3.6 VLM

定位：复杂中文研报的可选回退，不作为默认 OCR。

适合：

- 特殊多栏；
- 浮动文本框；
- 图表、注释、正文强关联；
- Reading Order 明显错误；
- Docling 和 GLM-OCR 均无法稳定还原的页面。

不适合：

- 所有财报页面默认走 VLM；
- 将原生 PDF 数字重新渲染后再生成；
- 直接承担后续财务指标抽取。

第一版提供手动 `mode=vlm`；自动 VLM fallback 默认关闭。

---

## 6. 解析模式与质量配置

### 6.1 `mode`

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `auto` | Docling Standard + GLM-OCR，必要时按质量规则回退 | 默认 |
| `standard` | Docling Standard + 非强制 GLM-OCR，不启用 VLM | 数字 PDF、普通财报 |
| `ocr` | Docling Standard + GLM-OCR Full Page OCR | 扫描件、图片 PDF |
| `vlm` | Docling VLM Pipeline + 远程 Ollama/Qwen | 特殊复杂版式 |

### 6.2 `profile`

| Profile | 重点 | 建议配置 |
|---|---|---|
| `fast` | 速度 | Table fast、低图片 scale、不启用 VLM fallback |
| `balanced` | 默认平衡 | Table accurate、按需 OCR、不启用自动 VLM |
| `accurate` | 质量 | Table accurate、较高 OCR scale、允许可配置 VLM fallback、较长超时 |

Profile 应映射到内部参数，不要把 Docling 的几十个参数直接暴露给普通用户。

### 6.3 自动回退规则

第一版只做保守规则，不做复杂模型分类器：

- 文档转换状态失败或部分失败；
- 页面输出为空或字符数低于阈值；
- Unicode 替换字符比例过高；
- 同一短片段异常重复；
- 表格导出为空且页面检测到明显表格区域；
- 后端明确返回超时或解析异常。

自动回退顺序：

```text
Docling Standard + GLM OCR
            │
            ├── 正常：直接输出
            │
            └── 异常：
                 ├── 若启用 GLM SDK fallback → 官方 GLM Pipeline
                 └── 若启用 VLM fallback     → Qwen VLM
```

不得因为“感觉页面复杂”就默认将所有页面发送给 27B VLM。

---

## 7. 项目目录结构

```text
docling-glm-parser/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 创建、路由挂载、静态资源
│   ├── config.py                  # Pydantic Settings
│   ├── lifespan.py                # 启停、Converter/Worker 初始化
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── documents.py           # 同步解析、异步任务创建
│   │   ├── jobs.py                # 查询、事件、结果、重试、删除
│   │   ├── backends.py            # 后端能力与可用性
│   │   ├── health.py              # health / ready
│   │   └── admin.py               # reload / cleanup
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── parse_options.py
│   │   ├── parse_result.py
│   │   ├── job.py
│   │   ├── backend.py
│   │   └── error.py
│   │
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── docling_standard.py
│   │   ├── glm_ocr_remote.py
│   │   ├── glm_sdk_remote.py      # v0.2
│   │   └── ollama_vlm.py          # 可选
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── parser_service.py
│   │   ├── job_service.py
│   │   ├── storage_service.py
│   │   ├── source_service.py
│   │   ├── export_service.py
│   │   ├── quality_service.py
│   │   └── cleanup_service.py
│   │
│   ├── repositories/
│   │   ├── __init__.py
│   │   └── job_repository.py      # SQLite
│   │
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── server.py
│   │
│   ├── security/
│   │   ├── auth.py
│   │   ├── source_url.py
│   │   └── file_validation.py
│   │
│   └── utils/
│       ├── ids.py
│       ├── page_range.py
│       ├── logging.py
│       └── timing.py
│
├── frontend/
│   ├── src/
│   │   ├── app/
│   │   ├── api/
│   │   ├── components/
│   │   ├── features/
│   │   │   ├── upload/
│   │   │   ├── preview/
│   │   │   ├── parse-options/
│   │   │   ├── jobs/
│   │   │   └── results/
│   │   ├── hooks/
│   │   ├── types/
│   │   └── styles/
│   ├── index.html
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
│
├── tests/
│   ├── fixtures/
│   │   ├── native-cn-report.pdf
│   │   ├── scanned-report.pdf
│   │   ├── mixed-report.pdf
│   │   ├── multi-column-research.pdf
│   │   └── table-report.pdf
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── scripts/
│   ├── benchmark.py
│   ├── download_docling_models.py
│   └── cleanup_jobs.py
│
├── docs/
│   ├── architecture.md
│   ├── api.md
│   ├── deployment.md
│   └── benchmark.md
│
├── static/                        # 前端构建产物，构建时生成/复制
├── data/                          # 默认本地数据目录，gitignore
├── .github/workflows/
│   ├── ci.yml
│   └── docker-publish.yml
├── .env.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── README.md
└── LICENSE
```

---

## 8. 后端技术选型

### 8.1 运行环境

推荐：

- Python 3.13：若直接依赖当前 `docling-glm-ocr` 插件；
- `uv`：依赖和虚拟环境管理；
- FastAPI；
- Uvicorn；
- Pydantic Settings；
- Docling；
- PyMuPDF：页数检查、页面预览辅助、必要时渲染；
- httpx：URL 下载、外部后端调用；
- aiosqlite：第一版任务元数据；
- aiofiles：异步文件读写；
- orjson：API 序列化；
- pytest、pytest-asyncio；
- Ruff；
- mypy 或 ty。

若不直接依赖 `docling-glm-ocr`，主服务可使用 Python 3.12；但项目初始化阶段建议统一采用 3.13，减少版本分支。

### 8.2 `pyproject.toml` 初始依赖示意

版本应在实际兼容性测试后锁定，不使用长期漂移的 `latest`：

```toml
[project]
name = "docling-glm-parser"
version = "0.1.0"
description = "Self-hosted document-to-Markdown/Text service powered by Docling and GLM-OCR"
requires-python = ">=3.13,<3.14"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "pydantic-settings",
  "python-multipart",
  "docling",
  "docling-glm-ocr",
  "pymupdf",
  "httpx",
  "aiofiles",
  "aiosqlite",
  "orjson",
  "tenacity",
]

[project.optional-dependencies]
mcp = ["mcp"]
dev = [
  "pytest",
  "pytest-asyncio",
  "pytest-cov",
  "ruff",
  "mypy",
]
```

### 8.3 Converter 生命周期

不要在每次请求中创建 `DocumentConverter`。

正确方式：

```text
应用启动
  ├── 加载配置
  ├── 检测外部 GLM/Ollama 后端
  ├── 按 mode/profile 创建 Converter
  ├── 每个解析 Worker 持有自己的 Converter
  └── 开始接收任务
```

默认 `PARSER_WORKERS=1`。增加并发时，每个 Worker 独立持有 Converter，避免假设上游对象一定线程安全。

---

## 9. 公共 API 设计

### 9.1 设计原则

- 不强行伪装成 OpenAI API；OpenAI 没有标准的 PDF-to-Markdown endpoint。
- 路径保持 `/v1` 版本化。
- 小文档支持同步；大文档默认异步。
- API 和 UI 共用同一任务服务。
- Markdown/Text 是内容输出；JSON 是协议封装。
- 公共响应不直接暴露 Docling 原始内部 Schema，避免上游升级导致接口破坏。

### 9.2 接口总览

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/v1/documents/parse` | 同步解析小文档 |
| `POST` | `/v1/documents/jobs` | 创建异步解析任务 |
| `GET` | `/v1/documents/jobs/{job_id}` | 查询任务状态 |
| `GET` | `/v1/documents/jobs/{job_id}/events` | SSE 进度事件 |
| `GET` | `/v1/documents/jobs/{job_id}/result` | 获取或下载结果 |
| `POST` | `/v1/documents/jobs/{job_id}/retry` | 重试失败任务/页码范围 |
| `DELETE` | `/v1/documents/jobs/{job_id}` | 取消或删除任务 |
| `GET` | `/v1/backends` | 查看 Docling、GLM、VLM 状态 |
| `GET` | `/health` | 存活检查 |
| `GET` | `/ready` | 就绪检查 |
| `POST/GET` | `/mcp` | MCP Streamable HTTP |
| `POST` | `/admin/reload` | 重建 Converter/刷新后端状态 |
| `POST` | `/admin/cleanup` | 清理过期任务 |
| `GET` | `/docs` | Swagger |
| `GET` | `/redoc` | ReDoc |
| `GET` | `/openapi.json` | OpenAPI Schema |

### 9.3 同步解析

```http
POST /v1/documents/parse
Content-Type: multipart/form-data
```

字段：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `file` | file | - | 与 `source_url` 二选一 |
| `source_url` | string | - | HTTP/HTTPS URL |
| `mode` | enum | `auto` | `auto/standard/ocr/vlm` |
| `profile` | enum | `balanced` | `fast/balanced/accurate` |
| `output_format` | enum | `markdown` | `markdown/text` |
| `page_range` | string | 全部 | 如 `1-5,8,10-12` |
| `language` | string | `zh,en` | OCR 语言提示 |
| `enable_vlm_fallback` | bool | `false` | 允许自动 VLM 回退 |
| `preserve_page_breaks` | bool | `true` | 保留页分隔符 |
| `include_pages` | bool | `false` | 返回逐页文本与状态 |
| `include_diagnostics` | bool | `true` | 返回告警与耗时 |
| `timeout_seconds` | int | 配置默认 | 单任务超时 |

同步接口限制：

- 默认只允许页数不超过 `SYNC_MAX_PAGES`；
- 默认文件不超过 `SYNC_MAX_BYTES`；
- 超出时返回 `409 sync_limit_exceeded`，并提示调用异步接口；
- 也可提供 `prefer_async=true`，直接返回任务 ID。

### 9.4 同步响应示例

```json
{
  "id": "docparse_01J...",
  "object": "document.parse",
  "status": "completed",
  "filename": "research-report.pdf",
  "mime_type": "application/pdf",
  "page_count": 42,
  "processed_pages": 42,
  "output_format": "markdown",
  "content": "# 公司研究报告\n\n...",
  "pipeline": {
    "mode": "auto",
    "profile": "balanced",
    "primary": "docling-standard",
    "ocr": "glm-ocr-remote",
    "vlm": null
  },
  "route_summary": {
    "native_text_pages": 36,
    "pages_with_ocr": 6,
    "ocr_regions": 19,
    "vlm_pages": 0,
    "failed_pages": 0
  },
  "warnings": [],
  "usage": {
    "input_bytes": 8231451,
    "duration_ms": 18432
  },
  "created_at": "2026-08-11T15:30:00Z"
}
```

注意：若上游组件无法提供可靠的逐页/逐区域来源统计，应返回 `null` 或省略字段，不能填入推测值。

### 9.5 异步任务创建

```http
POST /v1/documents/jobs
Content-Type: multipart/form-data
```

请求参数与同步接口一致。

响应：

```json
{
  "id": "job_01J...",
  "object": "document.parse.job",
  "status": "queued",
  "progress": {
    "current": 0,
    "total": 218,
    "unit": "page"
  },
  "status_url": "/v1/documents/jobs/job_01J...",
  "events_url": "/v1/documents/jobs/job_01J.../events",
  "result_url": "/v1/documents/jobs/job_01J.../result"
}
```

### 9.6 Job 状态

```text
queued
running
completed
partial
failed
cancelled
```

服务重启时，遗留的 `running` 任务应标记为 `failed`，错误码为 `server_restarted`；第一版不自动恢复执行，避免重复和状态不一致。

### 9.7 SSE 事件

事件类型：

```text
job.started
page.started
page.completed
page.warning
page.failed
job.progress
job.completed
job.failed
heartbeat
```

示例：

```text
event: job.progress
data: {"job_id":"job_01J...","current":18,"total":42,"percent":42.9}
```

SSE 只用于实时体验；任务状态仍以 `GET /jobs/{id}` 和 SQLite 为准。

### 9.8 结果下载

```http
GET /v1/documents/jobs/{job_id}/result?format=markdown&download=true
```

返回：

- Markdown：`text/markdown; charset=utf-8`；
- Text：`text/plain; charset=utf-8`；
- `Content-Disposition` 使用安全化后的原文件名。

### 9.9 统一错误格式

```json
{
  "error": {
    "code": "backend_unavailable",
    "message": "GLM-OCR backend is unavailable",
    "request_id": "req_01J...",
    "details": {
      "backend": "glm-ocr-remote"
    }
  }
}
```

建议状态码：

| HTTP | 场景 |
|---:|---|
| 400 | 参数冲突、URL 非法、页码范围非法 |
| 401 | API Key 无效 |
| 404 | Job 不存在 |
| 409 | 同步限制、状态冲突 |
| 413 | 文件过大 |
| 415 | 文件类型不支持 |
| 422 | 参数校验失败 |
| 429 | 队列或并发已满 |
| 502 | GLM/Ollama 后端错误 |
| 504 | 文档或模型请求超时 |

---

## 10. 内部数据模型

### 10.1 `DocumentParseOptions`

```python
class DocumentParseOptions(BaseModel):
    mode: Literal["auto", "standard", "ocr", "vlm"] = "auto"
    profile: Literal["fast", "balanced", "accurate"] = "balanced"
    output_format: Literal["markdown", "text"] = "markdown"
    page_range: str | None = None
    language: list[str] = ["zh", "en"]
    enable_vlm_fallback: bool = False
    preserve_page_breaks: bool = True
    include_pages: bool = False
    include_diagnostics: bool = True
    timeout_seconds: int | None = None
```

实际代码中不要使用可变对象作为默认值，应使用 `default_factory`。

### 10.2 `PageParseResult`

```python
class PageParseResult(BaseModel):
    page_number: int
    status: Literal["completed", "warning", "failed"]
    backend: str | None
    content: str | None
    duration_ms: int
    warnings: list[ParseWarning]
```

### 10.3 `DocumentParseResult`

内部结果必须与 Docling 原始对象解耦：

```python
class DocumentParseResult(BaseModel):
    document_id: str
    filename: str
    mime_type: str
    page_count: int
    processed_pages: int
    markdown: str
    plain_text: str
    pages: list[PageParseResult]
    route_summary: RouteSummary
    warnings: list[ParseWarning]
    usage: ParseUsage
```

DoclingDocument 只在 Parser Adapter 内部存在，不进入公共 API Model。

---

## 11. Web UI 设计

### 11.1 技术方案

参考 `qwen3-embedding-openai` 的 Vite 独立前端和 FastAPI 托管构建产物方式，但新项目 UI 状态更复杂，建议使用：

- Vite；
- React；
- TypeScript；
- PDF.js / `pdfjs-dist`；
- `react-markdown`；
- `remark-gfm`；
- `rehype-sanitize`；
- TanStack Query：任务轮询和请求缓存；
- 原生 CSS Variables 或轻量组件体系。

第一版不要引入重量级富文本编辑器，也不要让 UI 承担文档编辑功能。

### 11.2 页面布局

桌面布局：

```text
┌─────────────────────────────────────────────────────────────┐
│ Docling GLM   [API Ready] [GLM Ready] [VLM Optional]  设置 │
├─────────────────────────────────────────────────────────────┤
│ 上传/URL  模式 质量 输出 页码范围  高级设置      [开始解析] │
├───────────────────────────┬─────────────────────────────────┤
│                           │                                 │
│ PDF / 图片预览             │ Markdown 预览                   │
│                           │                                 │
│ 页码跳转、缩放、旋转        │ Markdown / Text / 页面状态 Tab  │
│                           │                                 │
├───────────────────────────┴─────────────────────────────────┤
│ 总进度 18/42 | 当前页 | 后端 | 耗时 | 告警 | 取消/重试      │
└─────────────────────────────────────────────────────────────┘
```

移动端/窄屏：

```text
上传与参数
    ↓
预览 / 结果 Tab 切换
    ↓
任务进度与操作
```

### 11.3 触控设计

考虑平板和远程桌面使用：

- 分栏拖拽条的实际可点击区域不小于 12px；
- 按钮高度不小于 40px；
- 页码、缩放和 Tab 不依赖 hover；
- 支持双击恢复 50/50 分栏；
- 小屏自动切换为上下或 Tab 布局；
- 不使用过细滚动条作为主要交互控件。

### 11.4 上传区

功能：

- 拖拽文件；
- 文件选择；
- 粘贴 URL；
- 显示文件名、类型、大小和页数；
- 最近一次参数保存在浏览器本地；
- 不默认保存文档内容到浏览器长期存储。

### 11.5 参数区

普通用户只显示：

- Mode：Auto / Standard / OCR / VLM；
- Profile：Fast / Balanced / Accurate；
- Output：Markdown / Text；
- Page Range；
- Start。

高级设置折叠：

- OCR 语言；
- VLM fallback；
- 保留页分隔；
- 超时；
- URL 下载选项；
- 调试信息。

### 11.6 结果区

Tab：

1. **Markdown Preview**：渲染后的 Markdown；
2. **Plain Text**：等宽文本、可复制；
3. **Page Status**：页码、状态、耗时、告警；
4. **API Example**：自动生成当前参数对应的 curl/Python 示例。

结果操作：

- 复制；
- 下载 `.md`；
- 下载 `.txt`；
- 失败任务重试；
- 指定页重试；
- 清除当前任务。

### 11.7 页面联动

当用户点击 Page Status 中的页码：

- 左侧 PDF 跳到对应页；
- 右侧滚动到该页输出；
- 高亮当前页；
- 显示该页告警和解析耗时。

第一版不要求对 Markdown 中每个元素做 bbox 高亮；这是后续增强功能。

### 11.8 后端状态

顶部状态只显示真实健康检查：

```text
API Ready
Docling Ready
GLM Ready / Unavailable
VLM Ready / Disabled / Unavailable
Queue 1/8
```

不能仅凭配置了 URL 就显示 Ready，必须实际探测后端。

---

## 12. MCP 设计

### 12.1 Tools

#### `parse_document`

参数：

```text
source_url        URL，与 file_base64 二选一
file_base64       小文件 Base64
filename          Base64 输入时必填
mode              auto/standard/ocr/vlm
profile           fast/balanced/accurate
output_format     markdown/text
page_range        可选
enable_vlm_fallback
```

返回：

- 小文档直接返回文本；
- 超过 MCP Base64/结果大小限制时，返回 Job ID 和 HTTP 结果地址；
- 不允许通过 MCP 传入任意本地文件路径。

#### `get_document_job`

查询任务状态。

#### `get_document_result`

获取已完成任务的 Markdown/Text；大结果应提示使用 HTTP 下载。

### 12.2 Resources

```text
doclingglm://health
doclingglm://backends
doclingglm://usage
```

### 12.3 Prompts

```text
document_parse_workflow
```

提示客户端：

- 普通 PDF 使用 `auto`；
- 扫描件使用 `ocr`；
- 复杂视觉版式才使用 `vlm`；
- 本项目不负责指标抽取。

---

## 13. 任务、存储与并发

### 13.1 第一版存储

```text
/data/
├── jobs.db
└── jobs/
    └── job_01J.../
        ├── input/
        │   └── original.pdf
        ├── output/
        │   ├── result.md
        │   ├── result.txt
        │   └── metadata.json
        └── logs/
            └── warnings.json
```

SQLite 保存：

- Job ID；
- 文件元信息；
- 参数；
- 状态；
- 进度；
- 创建/开始/完成时间；
- 错误码；
- 结果文件路径；
- TTL。

### 13.2 任务执行

第一版：

- 一个 Uvicorn 进程；
- 一个内置 Job Queue；
- Docling 同步工作放入专用线程/Worker，不能阻塞 FastAPI 事件循环；
- 默认 `PARSER_WORKERS=1`；
- 外部 GLM 并发由插件和服务端共同限制；
- VLM 并发默认 1。

达到多实例部署需求后，再引入 Redis + RQ/Celery/Arq；第一版不提前增加分布式队列复杂度。

### 13.3 默认限制

建议初始值：

```text
MAX_UPLOAD_BYTES        = 200 MiB
MAX_DOCUMENT_PAGES      = 1000
SYNC_MAX_BYTES          = 20 MiB
SYNC_MAX_PAGES          = 10
MAX_QUEUED_JOBS         = 8
PARSER_WORKERS           = 1
JOB_RETENTION_HOURS     = 24
DOCUMENT_TIMEOUT        = 900 s
GLM_REQUEST_TIMEOUT     = 120 s
VLM_REQUEST_TIMEOUT     = 300 s
```

这些值必须通过环境变量调整，并在 `/health` 中显示非敏感配置。

---

## 14. 配置设计

`.env.example`：

```dotenv
# 服务
HOST=0.0.0.0
PORT=12303
TZ=Asia/Shanghai
LOG_LEVEL=INFO
JSON_LOGS=0
DATA_DIR=/data

# 鉴权
API_KEY=
ADMIN_TOKEN=change-me
CORS_ORIGINS=

# 文件与任务
MAX_UPLOAD_BYTES=209715200
MAX_DOCUMENT_PAGES=1000
SYNC_MAX_BYTES=20971520
SYNC_MAX_PAGES=10
MAX_QUEUED_JOBS=8
PARSER_WORKERS=1
JOB_RETENTION_HOURS=24
DOCUMENT_TIMEOUT_SECONDS=900

# Docling
DOCLING_DEVICE=cpu
DOCLING_ARTIFACTS_PATH=/models/docling
DOCLING_TABLE_MODE=accurate
DOCLING_FORCE_BACKEND_TEXT=0
DOCLING_MODEL_DOWNLOAD=1

# GLM-OCR remote plugin
GLM_OCR_ENABLED=1
GLM_OCR_API_URL=http://glm-ocr:8000/v1/chat/completions
GLM_OCR_API_KEY=
GLM_OCR_MODEL=zai-org/GLM-OCR
GLM_OCR_LANG=zh,en
GLM_OCR_SCALE=3.0
GLM_OCR_MAX_IMAGE_PIXELS=4500000
GLM_OCR_MAX_CONCURRENCY=4
GLM_OCR_MAX_TOKENS=16384
GLM_OCR_TIMEOUT_SECONDS=120
GLM_OCR_MAX_RETRIES=3

# 官方 GLM SDK fallback，v0.2
GLM_SDK_ENABLED=0
GLM_SDK_URL=http://glm-ocr-sdk:5002/glmocr/parse

# Ollama VLM，默认关闭
VLM_ENABLED=0
VLM_BASE_URL=http://5090-host:11434/v1
VLM_API_KEY=ollama
VLM_MODEL=qwen3.6:35b
VLM_MAX_CONCURRENCY=1
VLM_TIMEOUT_SECONDS=300
VLM_TEMPERATURE=0

# URL 安全
ALLOW_SOURCE_URLS=1
ALLOW_PRIVATE_SOURCE_URLS=0
SOURCE_URL_MAX_REDIRECTS=3
SOURCE_URL_TIMEOUT_SECONDS=30
```

配置原则：

- 所有后端 URL 都可指向远端机器；
- 不把 Token 写进 Dockerfile；
- `/health` 不返回 API Key/Admin Token；
- 配置非法时启动失败，不静默降级；
- GLM 或 VLM 不可用时，服务可启动，但 `/ready` 和 `/backends` 必须准确显示能力缺失。

---

## 15. Docker 与部署

### 15.1 主服务 Dockerfile

沿用 `qwen3-embedding-openai` 的多阶段思路，但主服务不使用 vLLM 基础镜像：

```dockerfile
FROM node:20-bookworm-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.13-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 按 Docling/PDF 依赖的实际需要安装最小系统包
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN pip install uv \
    && uv sync --frozen --no-dev

COPY app ./app
COPY --from=frontend-builder /frontend/dist ./static

EXPOSE 12303
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "12303"]
```

实际实现时，需要根据 Docling 和所选 PDF backend 的运行依赖补充系统包；不得为了省事直接使用包含完整 CUDA 的大镜像。

### 15.2 Compose 拓扑

推荐将主解析服务和 GLM 模型服务分开：

```text
Docker Compose / 内网

parser:12303
├── CPU
├── FastAPI + UI + Docling
├── /data
└── /models/docling

         HTTP
          │
          ▼

glm-ocr:8000
├── GPU 5060Ti/2080Ti
├── vLLM
└── zai-org/GLM-OCR

         HTTP（可选，外部已有服务）
          │
          ▼

ollama:11434
└── GPU 5090 + qwen3.6:35b
```

`docker-compose.yml` 示例骨架：

```yaml
services:
  parser:
    build: .
    image: ghcr.io/scisaga/docling-glm-parser:latest
    container_name: docling_glm_parser
    restart: unless-stopped
    ports:
      - "12303:12303"
    env_file:
      - .env
    volumes:
      - ./data:/data
      - ./models/docling:/models/docling
    depends_on:
      glm-ocr:
        condition: service_healthy
        required: false
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:12303/health"]
      interval: 30s
      timeout: 5s
      retries: 5

  glm-ocr:
    profiles: ["glm"]
    image: ${GLM_VLLM_IMAGE}
    restart: unless-stopped
    ipc: host
    ports:
      - "8001:8000"
    volumes:
      - ./models/huggingface:/root/.cache/huggingface
    command:
      - zai-org/GLM-OCR
      - --port
      - "8000"
      - --trust-remote-code
      - --max-num-batched-tokens
      - "8192"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["0"]
              capabilities: [gpu]
```

`GLM_VLLM_IMAGE` 必须在目标显卡上验证后锁定具体版本，不在文档里长期写死 `latest`。

### 15.3 部署模式

#### 模式 A：只启动主服务

适合数字原生 PDF：

```bash
docker compose up -d parser
```

#### 模式 B：主服务 + GLM-OCR

```bash
docker compose --profile glm up -d
```

#### 模式 C：主服务 + 远程 GLM + 现有 Ollama

主服务 `.env` 指向远程地址，不由本项目管理模型进程。

---

## 16. 安全设计

### 16.1 文件上传

- 根据文件头检查 MIME，不只相信扩展名；
- 文件名做路径安全化；
- 禁止压缩炸弹和嵌套归档；
- 限制文件大小和页数；
- 每个任务独立目录；
- 不执行 PDF 内嵌脚本；
- 解析临时文件按 TTL 清理；
- 结果 Markdown 渲染时必须 sanitize HTML。

### 16.2 URL 获取与 SSRF

默认：

- 只允许 HTTP/HTTPS；
- 禁止 `file://`、`ftp://` 等协议；
- 禁止访问 localhost、link-local、metadata 地址和内网 IP；
- DNS 解析后再次验证目标 IP；
- 重定向每一跳都重新检查；
- 限制重定向次数、下载字节数和超时；
- 内网使用确有需要时，通过 `ALLOW_PRIVATE_SOURCE_URLS=1` 显式开启。

### 16.3 外部模型服务

- GLM/Ollama URL 由服务端配置，客户端不能在请求中任意指定；
- API Key 不写入日志；
- 远程后端失败时返回明确告警，不无限重试；
- `admin/reload` 与普通 API Key 分离；
- 默认关闭跨域，按部署需要配置 CORS。

---

## 17. 日志、监控与可观测性

### 17.1 日志字段

结构化日志至少包含：

```text
timestamp
level
request_id
job_id
filename_hash
page_number
parser_mode
backend
duration_ms
status
error_code
```

不要在普通日志中记录完整文档正文。

### 17.2 Health 与 Ready

`GET /health`：

- API 进程存活；
- 版本；
- uptime；
- 队列长度；
- 非敏感配置；
- 后端状态摘要。

`GET /ready`：

- SQLite 可写；
- 数据目录可写；
- Docling Converter 初始化成功；
- 当前默认 mode 所需的后端可用。

### 17.3 后续 Prometheus 指标

第二阶段可增加：

```text
document_parse_requests_total
document_parse_failures_total
document_parse_duration_seconds
document_pages_total
document_ocr_regions_total
document_vlm_pages_total
job_queue_depth
backend_request_duration_seconds
backend_errors_total
```

---

## 18. 测试与质量基准

### 18.1 单元测试

- 配置校验；
- 页码范围解析；
- 文件类型和大小校验；
- URL SSRF 校验；
- Job 状态机；
- Markdown/Text Normalizer；
- 错误格式；
- 自动回退规则；
- TTL 清理。

### 18.2 集成测试

- 数字原生中文财报；
- 扫描 PDF；
- 数字与扫描混合 PDF；
- 国内券商多栏研报；
- 复杂表格；
- 图片上传；
- GLM 后端超时；
- GLM 后端 4xx/5xx；
- Ollama 后端不可用；
- 同步限制转异步；
- Job 取消和重试；
- SSE 断线重连。

CI 中不实际启动大模型，应使用 Mock Server 模拟 GLM/Ollama；真实 GPU 回归测试在专用环境执行。

### 18.3 Golden Test

对固定样本文档保存期望结果，至少比较：

- 标题和段落顺序；
- 数字保真；
- 表格行列结构；
- 空页率；
- 重复片段率；
- Markdown 合法性；
- 公式和特殊字符；
- 页码范围准确性。

不要只比较全文字符串完全相等，因为上游模型版本可能产生合理的格式变化。应结合规则、关键字段和局部 diff。

### 18.4 Benchmark 指标

```text
Document Success Rate
Page Success Rate
Native Text Preservation Rate
Number Exact Match Rate
Table Cell Structure Accuracy
Empty Page Rate
Repeated Text Rate
Mean Seconds/Page
P95 Seconds/Page
Peak RAM
GLM Call Count
VLM Fallback Rate
```

财报和研报场景最重要的指标是：

1. 数字保真；
2. 阅读顺序；
3. 表格结构；
4. 空页/漏页；
5. 每页耗时。

---

## 19. GitHub Actions

### 19.1 `ci.yml`

触发：

- Pull Request；
- push 到 `main`；
- 手动触发。

步骤：

1. 安装 Python/uv；
2. Ruff；
3. 类型检查；
4. pytest；
5. 安装 Node；
6. 前端 lint/test/build；
7. 构建 Docker 镜像但不推送；
8. 检查 OpenAPI Schema 可生成。

### 19.2 `docker-publish.yml`

沿用现有项目模式：

- `main` 推送 `latest`；
- `v*` 标签推送版本标签；
- commit SHA 标签；
- Docker Buildx 缓存；
- GHCR；
- PR 仅构建、不推送；
- 生成 SBOM 可作为后续增强。

---

## 20. 版本规划

### v0.1：可用闭环

必须完成：

- 自研 FastAPI；
- 自研 Vite/React UI；
- 文件上传和 URL；
- Docling Standard；
- 远程 GLM-OCR 插件；
- `auto/standard/ocr`；
- 手动 `vlm` 可选；
- Markdown/Text；
- 同步和异步任务；
- 本地 SQLite + 文件存储；
- SSE 进度；
- Health/Ready/Backends；
- MCP；
- Docker/Compose；
- CI/GHCR；
- 核心测试和示例文档。

### v0.2：复杂文档增强

- 官方 GLM-OCR SDK Pipeline fallback；
- 自动页级质量检测；
- 页级 GLM/VLM 重试；
- PDF 页与输出页联动；
- 更完整的解析诊断；
- Prometheus；
- 基准测试面板。

### v0.3：规模化

- Redis + 分布式 Worker；
- 批量上传；
- 多解析节点调度；
- 持久化任务历史；
- Webhook；
- 配额和限流；
- 可插拔其他 OCR/VLM 后端。

---

## 21. v0.1 验收标准

项目初始化完成必须满足：

1. `docker compose up -d` 可启动 CPU 主服务；
2. `/` 可以上传 PDF/图片并显示解析结果；
3. `/docs`、`/redoc`、`/health`、`/ready` 可用；
4. 数字原生 PDF 在 GLM 不可用时仍可解析；
5. 扫描件在 GLM 可用时能够输出文本；
6. GLM 不可用时返回清晰告警，不导致服务崩溃；
7. 小文件可同步解析，大文件可异步处理；
8. 任务进度可轮询并通过 SSE 查看；
9. 结果可下载为 `.md` 和 `.txt`；
10. UI 能显示 API、Docling、GLM、VLM 的真实状态；
11. API Key 和 Admin Token 可选配置；
12. URL 输入具备 SSRF 防护；
13. 后端测试通过，前端能完成生产构建；
14. Docker 镜像可由 GitHub Actions 发布到 GHCR；
15. README 明确本项目不做指标抽取，也不是 Docling/GLM 官方项目。

---

## 22. README 首页建议文案

````markdown
# Docling GLM

自托管的文档转 Markdown/Text 微服务。

Docling GLM 使用 Docling 解析数字原生 PDF，并可通过远程 GLM-OCR
识别扫描页和图片区域；对于特殊复杂版式，可选接入 Ollama Vision
模型作为回退。项目提供 FastAPI、Web UI、异步任务、MCP、Docker
和 GHCR 镜像，适合内网财报、研报和通用文档解析。

> 本项目只负责文档内容解析，不负责财务指标抽取、实体关系抽取、
> RAG 或文档问答。

## Features

- PDF / PNG / JPEG / WEBP / TIFF
- Markdown / Plain Text
- Docling Standard Pipeline
- Remote GLM-OCR
- Optional Ollama VLM fallback
- Sync / Async jobs and SSE progress
- Web UI / OpenAPI / MCP
- Docker Compose / GHCR

## Quick Start

```bash
cp .env.example .env
docker compose up -d --build
```

Open:

- Web UI: `http://localhost:12303/`
- Swagger: `http://localhost:12303/docs`
- Health: `http://localhost:12303/health`
- MCP: `http://localhost:12303/mcp`
````

---

## 23. 可直接交给编码智能体的项目初始化提示词

以下提示词用于在空仓库中初始化第一版项目。执行时应将本设计文档一并放入仓库 `docs/project-spec.md`，作为实现依据。

```text
你是一名资深 Python/FastAPI、Docling、OCR 推理服务和 React/TypeScript
工程师。请在当前空仓库中创建一个可运行、可测试、可通过 Docker 部署的
开源项目。

项目显示名称：Docling GLM
仓库名称：docling-glm-parser
Python 包名：docling_glm_parser
默认端口：12303
许可证：Apache-2.0

项目目标：
将 PDF 和图片转换为 Markdown 或 Plain Text。项目只做文档内容解析，
禁止实现财务指标抽取、实体关系抽取、RAG、Embedding、向量数据库、
文档总结或问答。

总体方案：
1. 自研 FastAPI API 和 Web UI，不使用 docling-serve。
2. 主服务直接使用 docling Python SDK。
3. 默认采用 Docling Standard Pipeline。
4. OCR Engine 通过 docling-glm-ocr 或等价的封装调用远程
   zai-org/GLM-OCR OpenAI-compatible endpoint。
5. 可选实现 Docling VLM Pipeline Adapter，通过远程 Ollama
   qwen3.6:35b 处理 mode=vlm；默认关闭自动 VLM fallback。
6. 主服务必须能在无 GPU 环境运行。GLM-OCR 和 Ollama 均为外部 HTTP
   后端，不在主服务进程中启动。
7. 输出内容格式只支持 markdown 和 text；API 使用 JSON 封装状态和结果。

工程要求：
- Python 3.13、uv、pyproject.toml。
- FastAPI + Uvicorn + Pydantic Settings。
- 使用 app/ 包结构，不允许把主要逻辑堆入单个 app.py。
- DocumentConverter 在应用/Worker 启动时初始化并缓存，不得每次请求重建。
- 默认单 Parser Worker；Docling 同步处理不得阻塞 FastAPI 事件循环。
- 使用 SQLite + 本地文件目录实现第一版异步任务。
- 支持任务状态 queued/running/completed/partial/failed/cancelled。
- 支持 SSE 进度，但 GET Job 状态是持久化事实来源。
- 服务重启后将遗留 running 任务标记为 failed/server_restarted。
- 外部后端必须有健康探测、超时、并发限制、重试和错误映射。
- 不允许返回伪造 confidence；不可观测字段返回 null 或省略。

API 必须实现：
- POST /v1/documents/parse
- POST /v1/documents/jobs
- GET /v1/documents/jobs/{job_id}
- GET /v1/documents/jobs/{job_id}/events
- GET /v1/documents/jobs/{job_id}/result
- POST /v1/documents/jobs/{job_id}/retry
- DELETE /v1/documents/jobs/{job_id}
- GET /v1/backends
- GET /health
- GET /ready
- POST/GET /mcp
- POST /admin/reload
- POST /admin/cleanup
- GET /docs、/redoc、/openapi.json

解析参数：
- file 或 source_url，二选一
- mode=auto|standard|ocr|vlm
- profile=fast|balanced|accurate
- output_format=markdown|text
- page_range
- language，默认 zh,en
- enable_vlm_fallback，默认 false
- preserve_page_breaks
- include_pages
- include_diagnostics
- timeout_seconds

安全要求：
- 校验真实 MIME、文件大小、页数和安全文件名。
- URL 仅允许 HTTP/HTTPS。
- 默认禁止 localhost、私网、link-local 和云 metadata 地址。
- 每次重定向都重新执行 SSRF 检查。
- 限制下载大小、超时和重定向次数。
- Markdown 预览必须 sanitize HTML。
- API_KEY 可选；ADMIN_TOKEN 独立。
- 日志不得记录完整文档正文和密钥。

前端要求：
- Vite + React + TypeScript。
- FastAPI 在生产环境托管 frontend/dist。
- 使用 PDF.js 预览 PDF。
- 使用 react-markdown + remark-gfm + rehype-sanitize 渲染结果。
- 桌面端左右分栏：左侧 PDF/图片，右侧 Markdown/Text。
- 窄屏改为 Preview/Result Tab。
- 分栏拖拽热区至少 12px，支持触控。
- 顶部显示 API、Docling、GLM、VLM 和队列真实状态。
- 支持拖拽上传、URL、mode、profile、output、page_range。
- 支持总体进度、页面状态、取消、重试、复制、下载 .md/.txt。
- 提供 API Example 面板，按当前 UI 参数生成 curl 和 Python 示例。
- v0.1 不实现富文本编辑器、不实现用户账户、不实现长期文档库。

项目结构应至少包含：
- app/api
- app/models
- app/parsers
- app/services
- app/repositories
- app/mcp
- app/security
- frontend
- tests/unit
- tests/integration
- tests/e2e
- docs
- scripts
- Dockerfile
- docker-compose.yml
- .env.example
- .github/workflows/ci.yml
- .github/workflows/docker-publish.yml
- README.md
- LICENSE

Docker 要求：
- 多阶段构建：Node 构建前端，Python slim 运行主服务。
- 主镜像不包含 CUDA，不使用 vLLM 基础镜像。
- docker-compose 中 parser 与 glm-ocr 是独立服务。
- glm-ocr 通过 profile=glm 可选启动。
- Ollama 默认作为外部现有服务，只通过环境变量配置。
- 所有模型镜像和 Python 依赖在测试后锁定具体版本，不使用 latest 作为生产基线。

测试要求：
- pytest 单元和集成测试。
- CI 使用 Mock GLM/Ollama Server，不下载或运行大模型。
- 覆盖配置、文件校验、SSRF、页码范围、Job 状态机、错误格式、
  同步/异步接口、SSE、后端超时、结果下载和清理。
- 前端至少完成 typecheck、lint、build 和关键组件测试。
- 提供 tests/fixtures 的说明，但不要提交有版权风险的大型财报原文；
  使用自制、公开许可或极小测试文档。

实现质量要求：
- 代码必须可运行，不接受只创建目录或大量 TODO 占位。
- 所有配置集中在 Pydantic Settings。
- 所有公共模型带类型和字段说明，使 Swagger 可直接使用。
- 错误使用统一 JSON 结构。
- 为核心服务写中文注释，但标识符使用清晰英文。
- README 给出 CPU-only、外部 GLM、可选 Ollama 三种部署方式。
- 完成后实际运行后端测试、前端构建和 Docker build；修复失败后再总结。
- 不要实现本需求之外的指标抽取、RAG 或知识库功能。
```

---

## 24. 最终决策摘要

项目应采用：

```text
自研 API/UI/任务层
        +
Docling Python SDK
        +
远程 GLM-OCR 作为 OCR Engine
        +
可选 Ollama/Qwen VLM fallback
```

不采用：

```text
Fork Docling Serve
直接修改其 Gradio UI
所有页面默认走 27B VLM
在同一个容器内同时启动 FastAPI、Docling、vLLM 和 Ollama
把指标抽取并入当前项目
```

名称方面：

- **Docling GLM**：适合作为产品名称；
- **docling-glm-parser**：更适合作为公开仓库名称；
- **docling-glm**：内部项目可用，但公开发布时辨识度和边界略差。
