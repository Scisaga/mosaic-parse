# MosaicParse HTTP 与 MCP 契约

默认 Base URL 为 `http://localhost:12303`。在线定义以 `/openapi.json`、`/docs`
和 `/redoc` 为准。MosaicParse 只产出可追溯解析结果和派生渲染，不执行
Embedding、切块、索引、问答或领域实体抽取。

## 认证

设置 `API_KEY` 后，`/v1/content/*`、`/v1/backends` 与 `/mcp` 接受：

```http
Authorization: Bearer <API_KEY>
```

也可使用 `X-API-Key`。管理端点只接受独立的 `X-Admin-Token`。资产 URL 与
bundle 不会绕过 API Key。

## 输入与参数

`POST /v1/content/parse` 和 `POST /v1/content/jobs` 使用
`multipart/form-data`。`file` 与 `source_url` 必须且只能提供一个。

| 字段 | 值 | 默认值 |
|---|---|---|
| `file` | PDF、DOCX、PPTX、PNG、JPEG、WebP、TIFF、BMP、MP4、MOV、MKV、WebM、AVI | — |
| `source_url` | 通过 SSRF 校验的 HTTP(S) URL | — |
| `profile` | `fast\|balanced\|accurate` | `balanced` |
| `unit_range` | 一基页/幻灯片范围，如 `1-5,8` | 全部 |
| `language` | OCR 语言，逗号分隔 | `zh,en` |
| `description_language` | `zh-CN\|en\|auto` | `zh-CN` |
| `include_renderings` | 是否返回 Markdown/Text 投影视图 | `true` |
| `timeout_seconds` | `1..86400` | 服务默认值 |
| `prefer_async` | 仅 `/parse`；强制返回持久 Job | `false` |

`unit_range` 的语义由真实格式决定：PDF/TIFF 是页，PPTX 是幻灯片；DOCX 和
单帧图片只接受省略或 `1`；视频不接受范围。服务以 magic、OOXML 内容类型及
FFprobe 结果为准，不信任上传的 Content-Type。

`profile` 是唯一请求级质量控制。模型、后端 URL 与 prompt 均不能由请求指定。
旧 `mode`、`output_format`、`vlm_policy`、`enable_vlm_fallback`、
`preserve_page_breaks`、`include_pages`、`include_diagnostics` 会返回 422。

## 同步与自动异步

```bash
curl --fail-with-body http://localhost:12303/v1/content/parse \
  -H "Authorization: Bearer $API_KEY" \
  -F file=@photo.webp \
  -F profile=balanced \
  -F description_language=zh-CN
```

小型 PDF、图片、DOCX 和 PPTX 返回 HTTP 200 `ContentParseResult`，同时仍创建
保留 24 小时的持久 Job。独立视频、`prefer_async=true`、超过同步字节或单元限制
的输入返回 HTTP 202 `JobResponse`。因此调用方必须同时处理 200 与 202。

`POST /v1/content/jobs` 始终返回 202。视频只分析采样帧，不提取音轨，也不做
ASR。文档内嵌视频完全忽略；文档内嵌图片会成为资产。

## ContentParseResult 1.0

主结果的固定标识是：

```json
{
  "object": "content.parse_result",
  "schema_version": "content-parse-result/1.0",
  "status": "completed",
  "source": {
    "content_id": "job_...",
    "source_sha256": "...",
    "filename": "slides.pptx",
    "mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "kind": "pptx",
    "size_bytes": 12345,
    "unit_count": 3,
    "page_count": null,
    "slide_count": 3,
    "duration_ms": null,
    "width": null,
    "height": null
  },
  "units": [],
  "assets": [],
  "tables": [],
  "logical_tables": [],
  "visual_analysis": null,
  "video_analysis": null,
  "renderings": {"markdown": "...", "plain_text": "..."},
  "diagnostics": {},
  "warnings": [],
  "runtime": {},
  "links": {},
  "created_at": "2026-08-19T00:00:00Z"
}
```

`units[].unit_type` 是 `page`、`slide`、`document_body`、`image` 或 `video`。
每个单元可含 regions、blocks、tables 引用、asset 引用、渲染与实测诊断。表格
单元格保留 row/column、span、bbox、所选文本、来源追踪和 reason code。

独立图片的结构化说明位于顶层 `visual_analysis`，并同时附在源图片资产上；文档
图片说明位于对应 `assets[].visual_analysis`。重复图片以 SHA256 去重，多个出现位置
记录在 `locations[]`。`mixed` 图片同时保留文档结构与视觉描述。

`video_analysis` 包含实测时长/尺寸/编码、场景区间和单调关键帧时间戳。摘要只能
声称采样帧可见的内容；`visual_only=true` 明确表示没有分析音频。

`include_renderings=false` 只清空顶层和单元级 Markdown/Text，解析结构、图片描述、
关键帧、诊断和告警仍保留。未知测量使用 `null`，不能以零冒充测量值。

## Job、SSE 与结果

```text
POST   /v1/content/jobs
GET    /v1/content/jobs/{id}
GET    /v1/content/jobs/{id}/events
GET    /v1/content/jobs/{id}/result
GET    /v1/content/jobs/{id}/rendering/{markdown|text}
POST   /v1/content/jobs/{id}/retry
DELETE /v1/content/jobs/{id}
```

状态流转：

```text
queued -> running -> completed
                  -> partial
                  -> failed
queued/running    -> cancelled
```

SSE 进度单位可能为 `page`、`slide`、`asset` 或 `frame`。SSE 是增量体验层；断线后
应重新读取 Job 状态。`partial` 表示父内容可用但至少一项非致命媒体处理失败，例如
文档图片的 VLM 不可用。纯视觉独立图片/视频缺少 VLM 时整个任务失败。

0.4.0 升级会按受保护清理流程删除旧 Job；若意外恢复旧结果文件，读取时返回 409
`legacy_result_contract`，不会静默转换。

## 资产与 bundle

```text
GET /v1/content/jobs/{id}/assets
GET /v1/content/jobs/{id}/assets/{asset_id}
GET /v1/content/jobs/{id}/bundle
```

资产元数据含 MIME、SHA256、字节数、宽高/时长、角色、父资产、出现位置、状态和
鉴权下载 URL。独立图片返回原始字节；DOCX/PPTX 返回原始嵌入图片；PDF 只返回
Docling 语义图片区域的页面裁剪；视频返回原视频和派生关键帧。

视频下载支持单个 HTTP byte range，并返回 `206`、`Content-Range` 和 SHA256
ETag。bundle 按需原子生成并缓存，包含 `manifest.json` 和所有可用资产；生成时会
重新核对每项 SHA256。

## MCP

`GET/POST /mcp` 暴露且只暴露：

- `parse_content`
- `get_content_job`
- `get_content_result`
- `get_content_rendering`
- `get_content_assets`

资源协议为 `mosaicparse://health`、`mosaicparse://backends`、
`mosaicparse://usage`。大结果返回 HTTP URL；图片、视频和关键帧不内联 Base64。

## 错误协议

```json
{
  "error": {
    "code": "validation_error",
    "message": "The request parameters are invalid",
    "request_id": "req_...",
    "details": {}
  }
}
```

常见状态：400（输入冲突/范围/URL）、401（认证）、404（Job/资产）、409（状态或
旧契约）、413（大小）、415（真实格式）、422（参数）、429（容量）、502（解析或
必需模型后端）、504（超时）。旧 `/v1/documents/*` 不注册并返回 404。
