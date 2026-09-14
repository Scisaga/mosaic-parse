# MosaicParse HTTP 与 MCP 契约

默认 Base URL 为 `http://localhost:12303`，在线定义以 `/openapi.json`、`/docs`
和 `/redoc` 为准。

MosaicParse 的文档结果只有一种：普通 GFM Markdown。任务状态、错误和资产清单仍使用
JSON 作为控制协议，但它们不是第二份文档解析结果。详细坐标、候选文本和单元格证据只在
请求内部使用，不通过结果 API 返回；资产清单也不包含页位置或 bbox。

## 认证

设置 `API_KEY` 后，`/v1/content/*`、`/v1/backends` 与 `/mcp` 接受：

```http
Authorization: Bearer <API_KEY>
```

也可使用 `X-API-Key`。管理端点只接受独立的 `X-Admin-Token`。资产 URL 与 bundle
不会绕过 API Key。

## 输入与参数

`POST /v1/content/parse` 和 `POST /v1/content/jobs` 使用 `multipart/form-data`；
`file` 与 `source_url` 必须且只能提供一个。

| 字段 | 值 | 默认值 |
|---|---|---|
| `file` | PDF、DOCX、PPTX、常见图片及视频 | — |
| `source_url` | 通过 SSRF 校验的 HTTP(S) URL | — |
| `scan_policy` | `auto\|skip` | `auto` |
| `unit_range` | 一基页/幻灯片范围，如 `1-5,8` | 全部 |
| `language` | OCR 语言，逗号分隔 | `zh,en` |
| `describe_images` | 是否为嵌入图片生成模型描述；资产始终保留 | `false` |
| `description_language` | `zh-CN\|en\|auto` | `zh-CN` |
| `timeout_seconds` | `1..86400` | 服务默认值 |
| `prefer_async` | 仅 `/parse`；强制返回持久 Job | `false` |

`scan_policy=auto` 按页面自动处理扫描件；`skip` 不执行 PDF 扫描 OCR/表格抽取，保留
整页图像资产并输出明确的跳过状态。旧 `profile`、`mode`、`output_format`、`vlm_policy`、
`enable_vlm_fallback`、`preserve_page_breaks`、`include_pages`、
`include_diagnostics` 和 `include_renderings` 均返回 422。

## 同步与异步

```bash
curl --fail-with-body http://localhost:12303/v1/content/parse \
  -H "Authorization: Bearer $API_KEY" \
  -F file=@report.pdf \
  -F scan_policy=auto
```

小输入成功时返回 HTTP 200：

```http
Content-Type: text/markdown; charset=utf-8
X-Content-ID: job_...
Location: /v1/content/jobs/job_.../result
```

响应体就是完整 GFM Markdown。视频、`prefer_async=true` 或超过同步限制的输入
返回 HTTP 202 `JobResponse`。`POST /v1/content/jobs` 始终返回 202；调用方轮询任务
到 `completed` 或 `partial` 后读取：

```http
GET /v1/content/jobs/{id}/result
Accept: text/markdown
```

`?download=true` 增加 `.md` 的 `Content-Disposition`。旧
`/rendering/{markdown|text}` 已移除。

跳过的扫描 PDF 页不会输出失去行列归属的 OCR 数字，而是输出：

```markdown
> 扫描页未处理（scan_policy=skip）：第 8 页，页面类型为 scanned。
```

对应整页图像可在结果末尾的“图片资产”列表或资产接口中取得。

## GFM Markdown 输出

输出忠实保留解析器恢复出的标题、段落、列表和表格，不再按财务、股东或键值等领域类型
二次改写。可确定属于同一逻辑表的跨页片段会先合并；续页重复表头会去重，但同一张表中
真正的多级表头或分段表头会原样保留。下游 LLM 因而可以结合上下文理解表头，而不是依赖
转换器把第一行硬套到所有数据行。

```markdown
## 截至报告期末的财务指标

单位：万元

| 项目 | 本报告期末 | 上年末 | 本报告期末比上年末增减 |
| --- | ---: | ---: | ---: |
| 流动比率 | 1.75 | 1.95 | -10.26% |
| 资产负债率 | 63.86% | 63.82% | 0.04% |
|  | 本报告期 | 上年同期 | 本报告期比上年同期增减 |
| 扣除非经常性损益后净利润 | 69,780.34 | 59,811.73 | 16.67% |
```

图片、图表、签章裁剪和视频关键帧始终保留为鉴权资产。`describe_images=true` 时，
嵌入图片的描述和可见文字使用普通 Markdown 列表追加：

```markdown
## 图片资产

- **figure-1.png**（embedded_image，第 3 页，资产 ID：`asset_...`）：[下载](/v1/content/jobs/.../assets/asset_...)
  - 描述：图片中可见的内容说明
  - 可见文字：图片内可见文字
```

## Job、SSE、资产与 bundle

```text
POST   /v1/content/jobs
GET    /v1/content/jobs/{id}
GET    /v1/content/jobs/{id}/events
GET    /v1/content/jobs/{id}/result
GET    /v1/content/jobs/{id}/assets
GET    /v1/content/jobs/{id}/assets/{asset_id}
GET    /v1/content/jobs/{id}/bundle
POST   /v1/content/jobs/{id}/retry
DELETE /v1/content/jobs/{id}
```

SSE 是增量体验层，断线后以 Job JSON 为准。资产元数据含 MIME、SHA256、尺寸、角色、
状态和下载 URL，不含页位置或 bbox；视频支持单个 HTTP byte range。bundle 包含内部
`manifest.json` 与资产文件，不包含另一份详细解析结果。

## MCP

`GET/POST /mcp` 暴露：

- `parse_content`
- `get_content_job`
- `get_content_result`
- `get_content_assets`

小结果在 MCP JSON 信封的 `content` 字段内携带 `text/markdown`；超过 MCP 字符限制
时返回同一个 HTTP 结果 URL。不存在单独的 rendering 工具。

## 错误协议

错误与任务控制仍使用 JSON：

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

常见状态：400（输入冲突/范围/URL）、401（认证）、404（Job/资产）、409（状态或旧
契约）、413（大小）、415（真实格式）、422（参数）、429（容量）、502（解析后端）、
504（超时）。
