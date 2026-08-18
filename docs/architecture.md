# Architecture

MosaicParse 是多模态内容解析层，不是业务信息抽取层。FastAPI 服务保持 CPU-only；
GLM-OCR 和 Qwen 是可选远程模型后端。

```text
Browser / HTTP / MCP
          |
          v
Source validation + jobs + timeout
          |
          v
Real-format router (magic / OOXML / FFprobe)
          |
          +--> PDF/image document -----> Docling + deterministic repair
          |
          +--> visual/mixed image ------> measured routing + VLM description
          |
          +--> DOCX/PPTX --------------> Docling + image-only OOXML relations
          |
          +--> standalone video --------> bounded FFmpeg keyframes + VLM
                                      |
                                      v
                        unit/region/table/asset result
                                      |
                                      v
                     ContentParseResult v1 (primary)
                           |                    |
                           v                    v
                  Markdown renderer      plain-text renderer
                           |
                           v
                  EventRail / other domain systems
                  entity + relation + event extraction
```

## 边界

本项目负责：

- 内容格式、页面/幻灯片/图片分类、旋转、区域和阅读顺序；
- 原生文字、OCR 与视觉读取的来源记录；
- 表格拓扑、单元格、span、bbox 与跨页逻辑表；
- 签章、印章、手写、图片资产与正文的分离；
- 独立视频的实测元数据、场景候选、关键帧与采样限定摘要；
- 结构质量、未解决冲突和运行测量；
- 从解析结果派生 Markdown / Plain Text。

本项目不负责：

- 公司、人物、产品或指标的规范化与消歧；
- `ReportedFact`、实体、关系、事件或产业图谱；
- 事件时间、参与者和数值角色的领域解释；
- Embedding、切块、索引、问答、领域总结和长期资产管理。

EventRail 应保存 `content_id/unit_id/region_id/table_id/cell_id/asset_id` 作为来源引用，
并独立演进领域 ontology。解析器升级不会直接改写 ER 的事件定义。

## 唯一公共路由

请求不再选择 `standard/ocr/vlm` 后端。`profile` 是唯一质量控制：

- `fast/balanced`：自动 Docling/GLM 路径，不出站调用 Qwen；
- `accurate`：只把测得的复杂扫描/混合表、横置表和签章页送入视觉融合。

运行时只保留 `VISUAL_ROUTER_ENABLED=0` 和 `VLM_ENABLED=0` 等部署级停用开关。
不存在整页 Qwen Markdown 替换，也不存在旧 fallback/diagram 分支。

## 视觉融合

GLM SDK 提供布局区域、HTML 表格和 OCR 原值；Docling 提供原生文本、表格与坐标；
Qwen 负责区域语义、方向、表格拓扑、行列归属、可见值读取和冲突裁决。表格按
区域和单元格装配，不能覆盖同页正文、图片或跨页来源。

单页最多 3 次 Qwen 请求、总预算 180 秒。区域读取最多 16K 输出 token，模型别名
使用 32K context。结构化响应经 JSON Schema 和 Pydantic 双重校验。reasoning 仅
用于模型内部推理与长度测量，不进入公共结果、API 或日志。

## ContentParseResult

稳定契约位于 `app/models/content_result.py`，版本为 `content-parse-result/1.0`。核心关系：

```text
content
source
  units[]
    regions[]  -> block_ids[] / table_ids[]
    blocks[]   -> bbox + reading_order + provenance
  tables[]
    cells[]    -> row/column/span/bbox/text/provenance
  logical_tables[] -> fragment_table_ids[] + source_units[]
  assets[]          -> SHA256 + locations[] + visual_analysis
  video_analysis    -> scenes[] + keyframes[]
  renderings        -> derived Markdown / plain text
  diagnostics       -> measured quality counts
  runtime           -> backend and latency measurements
```

未知坐标或测量保持 `null`，不能伪造为零。原始媒体通过鉴权资产接口返回；prompt、
reasoning 和未采用候选正文不进入公共结果或日志。

## 持久化

```text
/data
├── jobs.db
└── jobs/<job_id>
    ├── input/original.<ext>
    ├── output/result.json
    ├── output/rendered.md
    ├── output/rendered.txt
    ├── output/assets.zip
    ├── assets/original/*
    ├── assets/derived/keyframes/*
    └── logs/warnings.json
```

`result.json` 是主结果；两个 rendering 文件是便利视图。SQLite 是 Job 状态真值，
SSE 只是增量体验层。

## 生命周期与安全

启动时打开 SQLite、标记中断 Job、创建有界队列并初始化解析 worker。Docling 的
同步 CPU 转换在线程外执行；外部模型调用有独立超时、重试和并发限制。

上传字节与 URL 均不可信：校验 magic、OOXML 内容类型/关系、ZIP 展开边界、
FFprobe 元数据、大小、单元数、文件名、协议、解析 IP 和每次重定向。OOXML 只遍历
image relationship；video/media relationship 完全不进入处理或诊断。文档、FFmpeg
和 VLM 分别有并发边界。模型 URL、模型名和 prompt 只能由服务端配置。
