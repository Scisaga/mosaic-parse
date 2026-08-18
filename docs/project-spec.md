# MosaicParse 产品与工程规格

> 当前契约：`content-evidence/1.0`
>
> 服务端口：`12303`
>
> 主结果：`ContentEvidenceIR`

## 1. 产品目标

MosaicParse 把 PDF、DOCX、PPTX、图片和独立视频转换为可追溯的结构化内容证据。它回答：

- 页、幻灯片、图片或采样视频帧上有哪些结构与视觉元素；
- 它们位于哪里、阅读顺序、出现位置或时间戳如何；
- 值来自原生 PDF、Docling、GLM 还是 Qwen；
- 哪些结构完整、哪些仍有冲突或缺失。

Markdown / Plain Text 是从证据结构生成的便利视图，不是产品的语义终点。

## 2. 与 EventRail 的边界

MosaicParse 不定义领域事件。EventRail 负责：

- 实体识别、规范化、别名合并和消歧；
- 关系和事件 ontology；
- 将公司、期间、指标、数值、单位等证据组装为事件；
- 事件去重、修订、冲突和产业图谱。

EventRail 的事件应引用本项目的稳定证据 ID：

```text
content_id / unit_id / region_id / block_id / table_id / cell_id / asset_id
```

`ReportedFact` 可在 EventRail 中作为事件体系的事实陈述类型或事件观测，不进入
文档解析器。

## 3. 请求契约

同步和 Job 创建只接受：

```text
file | source_url
profile=fast|balanced|accurate
unit_range
language
description_language=zh-CN|en|auto
include_renderings
timeout_seconds
prefer_async                  # 仅 /parse
```

旧后端模式、输出格式、VLM policy、逐页/诊断开关全部删除且不兼容。

路由规则：

- `fast/balanced`：文档使用 Docling/GLM 自动证据路径；
- `accurate`：文档中的复杂视觉结构可执行区域视觉融合；
- 纯视觉图片和独立视频始终需要服务端配置的 VLM；
- 文档内嵌图片失败使父结果为 `partial`，内嵌视频完全忽略；
- `VISUAL_ROUTER_ENABLED=0` 或 `VLM_ENABLED=0` 是部署级总开关。

## 4. 主结果

`ContentEvidenceIR` 顶层字段：

```text
object = content.evidence
schema_version = content-evidence/1.0
status
source
units[]
assets[]
tables[]
logical_tables[]
visual_analysis
video_analysis
renderings
diagnostics
warnings[]
runtime
links
created_at
```

单元类型为 page、slide、document_body、image 或 video。页面/幻灯片包含区域、
文本块、物理表片段、bbox、阅读顺序、旋转、质量诊断和派生视图。资产保存 SHA256、
角色、父资产、出现位置、处理状态和鉴权 URL。视频分析只陈述采样关键帧可见内容，
不含音频证据。未知值使用 `null`；不得用零代表未知测量。

公共 IR 不保存候选正文、图像、prompt、reasoning 或业务推断。

## 5. 视觉融合

GLM SDK 提供布局、区域、HTML 表格与 OCR；Docling 提供原生文本、坐标和原生表格；
Qwen 提供方向、区域语义、拓扑、行列归属、可见值读取与冲突裁决。

约束：

- 不执行整页 Markdown 替换；
- 按区域和单元格装配；
- 左右并表生成独立 TableIR；
- 签章、印章、手写证据不混入打印正文；
- 单页最多 3 次 Qwen、180 秒；
- 32K context，规划 4K、区域 16K、冲突 8K 输出预算；
- JSON Schema 强制结构化响应，并经 Pydantic 再校验。

## 6. 质量语义

- `trusted`：结构完整，无截断、超时和未解决冲突；
- `degraded`：内容可用，但存在预算耗尽、单源字段或少量未复核冲突；
- `untrusted`：关键区域缺失、表格结构不完整或视觉重复未解决；
- `failed`：没有任何可用页面内容。

单元格的 `selected` 只表示由一个来源选中，不自动等价于页面 degraded。页面结论
由结构完整性和未解决问题决定。

## 7. 持久化和接口

```text
output/result.json     # 主结果
output/rendered.md     # 派生
output/rendered.txt    # 派生
output/assets.zip      # 按需原子生成并缓存
assets/original/*      # 原始图片、Office 图片和独立视频
assets/derived/keyframes/*
logs/warnings.json     # 无正文诊断
```

HTTP：

```text
POST /v1/content/parse
POST /v1/content/jobs
GET  /v1/content/jobs/{id}
GET  /v1/content/jobs/{id}/events
GET  /v1/content/jobs/{id}/result
GET  /v1/content/jobs/{id}/rendering/{markdown|text}
GET  /v1/content/jobs/{id}/assets
GET  /v1/content/jobs/{id}/assets/{asset_id}
GET  /v1/content/jobs/{id}/bundle
POST /v1/content/jobs/{id}/retry
DELETE /v1/content/jobs/{id}
```

MCP：`parse_content`、`get_content_job`、`get_content_evidence`、
`get_content_rendering`、`get_content_assets`；资源 scheme 为 `mosaicparse://`。

## 8. 验收

- Python：ruff、mypy、pytest、OpenAPI、fixture check；
- 前端：lint、typecheck、test、build；
- Compose：配置展开和健康检查；
- FFmpeg/FFprobe、Office、图片、视频和资产下载在真实镜像中闭环；
- 含内嵌视频 PPTX 不产生视频资产、视频告警或 FFmpeg 调用；
- 关键帧单调、位于实测时长内、不超过 24，并覆盖首尾；
- 资产下载字节与 SHA256 一致，视频 Range、bundle 与过期清理可验证；
- `balanced` 原生页不产生 Qwen 调用；
- `accurate` 复杂视觉页不超过 3 次/180 秒；
- IR 保留同页非表正文、图片/签章证据、cell span 和跨页 table provenance；
- 数字、符号、单位和日期不得相对真值退化；
- 私有 PDF、manifest 和运行结果不进入 Git。
