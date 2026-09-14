# MosaicParse 产品与工程规格

> 当前结果契约：GFM Markdown（`text/markdown`）
>
> 服务端口：`12303`
>
> 唯一文档结果：`result.md`

## 1. 产品目标

MosaicParse 把 PDF、DOCX、PPTX、图片和独立视频转换为一份适合 LLM/RAG/ER 消费的
GFM Markdown。它完成版面恢复、表格识别、跨页表合并和媒体引用，但不把表格按领域规则
改写成另一套事实语法，也不向下游返回解析坐标对象。

## 2. 输出语义

- 正文、标题、列表保持 Markdown；
- 表格统一保持 GFM，不执行财务、股东、键值或说明行的领域分类与改写；
- 可确定的跨页连续表在导出前合并，续页重复表头按结构证据去重；
- 多级表头和同表内的分段表头保持为表格行，不把首行表头硬套给后续所有行；
- 嵌入图片、图表、签章区域和视频关键帧保留为资产，并用普通 Markdown 列表引用；
- 详细 bbox、候选来源和融合证据仅存在于请求内部。

## 3. 与 EventRail 的边界

MosaicParse 只恢复可见标签、期间、单位和值在文档中的结构关系；EventRail 继续负责指标
别名归一、公司实体消歧、关系/事件 ontology、事件去重、修订和训练数据生命周期。
下游无需把 Evidence IR 放入 LLM 上下文。

## 4. 请求与路由

公共请求参数：

```text
file | source_url
scan_policy=auto|skip             # 默认 auto；skip 只保留扫描页图像
unit_range
language
describe_images=false           # 嵌入图片始终保留；描述按需生成
description_language=zh-CN|en|auto
timeout_seconds
prefer_async                  # 仅 /parse
```

`profile`、`include_renderings` 与旧后端选择参数已删除。路由原则：

- 数字 PDF：原生文本 + Docling Layout/TableFormer，不做无条件全页 OCR；
- 扫描/混合 PDF：PP-DocLayoutV3 + 按页面方向旋转后的 GLM-OCR；
- 干净财务表：GLM 直接采用，Qwen 调用为 0；
- 少量可定位的错列/数字冲突：相关行裁剪后一次 Qwen 校验；
- 多级表头、并表或全局拓扑失败：全表 Qwen 兜底；
- Office/PDF：始终保留嵌入图片资产，仅在 `describe_images=true` 时生成描述；
- 独立图片/视频：内容本体使用有界视觉分析。
- `scan_policy=skip`：PDF 扫描/混合页不输出 OCR 数值或表格事实，保留整页资产和显式状态。

## 5. 持久化和接口

```text
output/result.md         # 唯一文档结果
output/asset-index.json  # 无坐标资产控制元数据，无正文和表格 Evidence IR
output/assets.zip        # 按需生成
assets/original/*
assets/derived/keyframes/*
assets/derived/previews/*
logs/warnings.json       # 无正文诊断
```

HTTP：

```text
POST /v1/content/parse                  # 200 text/markdown 或 202 Job JSON
POST /v1/content/jobs
GET  /v1/content/jobs/{id}
GET  /v1/content/jobs/{id}/events
GET  /v1/content/jobs/{id}/result       # text/markdown
GET  /v1/content/jobs/{id}/assets
GET  /v1/content/jobs/{id}/assets/{asset_id}
GET  /v1/content/jobs/{id}/bundle
POST /v1/content/jobs/{id}/retry
DELETE /v1/content/jobs/{id}
```

MCP：`parse_content`、`get_content_job`、`get_content_result`、
`get_content_assets`。不存在 JSON 文档结果或独立 rendering 工具。

## 6. Markdown 导出原则

导出器只做格式级处理：页序拼接、可确定的跨页表合并、续页重复表头去重和 GFM
规范化。它不做指标别名、期间推断、数值归一化、表格业务分型或事实生成。识别不确定时
保留原始单元格文本和相邻上下文，让下游模型基于整段文档判断。

## 7. 质量和训练闭环

短期以规则门控和小范围视觉复核为主，不先微调大模型。标注数据应保存：输入页/裁剪、
模型候选、人工最终结构、原始单元格引用、错误类型和模型版本；训练/评测集合按文档
拆分，防止同一财报页面泄漏到训练与测试两侧。

只有在积累足够的真实纠错样本后，才分别训练版面/表格结构或字段归一化模型。LLM 可以
生成预标注，但必须经人工确认后进入 Gold；线上纠错进入候选池，不直接回灌训练集。

## 8. 验收

- 后端 ruff、mypy、pytest、OpenAPI 通过；前端测试、类型检查和构建通过；
- `/parse` 与 `/result` 只返回 `text/markdown`，文档持久化只有 `output/result.md`；
- UI 只有扫描页开关、内容预览、Markdown 源文、媒体和 API 示例，不提供质量档位或 JSON/TXT 下载；
- 数字 PDF 不无条件 OCR；干净 GLM 财务表 Qwen 调用为 0；
- 局部冲突不升级为整页/整表 Qwen，完整兜底仍受每页调用和时间预算约束；
- 多级/分段表头、跨页并表、续页表头、期间、负数、单位、附注与资产引用有回归测试；
- 私有 PDF、Gold 标注和运行结果不进入 Git。
