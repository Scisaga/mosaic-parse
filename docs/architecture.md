# Architecture

MosaicParse 是 PDF/Office/图片/视频到 GFM Markdown 的解析层。详细坐标证据是
请求内的解析中间表示，不是交给业务 LLM 的输出；EventRail 等下游直接消费可读正文、
列表和表格，再完成实体、关系、事件与财务指标抽取。

```text
Browser / HTTP / MCP
          |
          v
格式校验 + Job + 页范围 + 超时
          |
          +-- 数字 PDF ------> 原生文本 + Docling Layout/TableFormer
          |
          +-- 扫描/混合 PDF -> PP-DocLayoutV3 + 定向 GLM-OCR
          |                         |
          |                         v
          |                   财务表质量门控
          |                    |      |       |
          |                    |      |       +-- 全表 Qwen（拓扑失败）
          |                    |      +---------- 局部 Qwen（冲突行）
          |                    +----------------- GLM 直接采用
          |
          +-- Office ---------> 原生结构 + 嵌入图片关系
          +-- 图片/视频 ------> 资产保留 + 按需视觉描述/关键帧
                                      |
                                      v
                      内部 Evidence IR + TableGridIR
                                      |
                                      v
                  跨页表合并 + GFM Markdown 导出
                    + 独立鉴权资产清单/下载
```

## PDF 路由

文字型 PDF 不默认走 GLM-OCR。原生文本坐标可靠时，Docling Layout/TableFormer 直接
恢复阅读顺序和表格；OCR 只用于扫描页、混合页或实测质量不足的区域。

默认 `scan_policy=auto` 下，扫描表格先按检测方向旋转页面像素，再执行
PP-DocLayoutV3 与 GLM-OCR。
GLM 表格经过确定性门控：

1. 归一化左右并表、空行和可确定的附注括号；
2. 验证表头/列宽/期间列、附注进入金额列等结构不变量；
3. 使用小计、资产负债恒等式和签章遮挡位置发现可局部定位的数字冲突；
4. 无冲突时零次调用 Qwen；少量冲突只裁剪相关行并至多调用一次 Qwen；只有拓扑不明、
   多级表头失败或全局恒等式无法定位时才执行全表视觉解析。

`scan_policy=skip` 在 PDF 进入 Docling 时关闭扫描 OCR，检测到的扫描/混合页不产生
表格内容，只保留可信原生文字、明确的 Markdown 跳过提示和整页图像资产。
`VLM_ENABLED=0` 是部署级硬停用；此时可用 GLM 结果仍会保留，并标注局部未复核状态。

## 内部证据与公共输出

内部 `ContentParseResult`/Evidence IR 保存页、区域、候选来源、bbox、表格单元格和质量，
仅用于同一次解析中的融合、质量判断、跨页关系和资产裁剪。它不进入 OpenAPI，不作为
结果文件持久化，也不发给下游 LLM。

公共 GFM Markdown 包含：

- 标准 Markdown 正文、标题和列表；
- 解析器识别出的 GFM 表格，包括多级/分段表头；
- 合并后的跨页逻辑表，续页重复表头仅在结构证据充分时去重；
- 图片/视频资产的普通 Markdown 列表、描述和下载链接。

解析层不判断某张表应变成财务事实、公司字段或股东关系，也不依据固定首行生成字段名。
公司实体消歧、关系/事件 ontology、财务指标归一化、事件去重和修订由 EventRail 或其他
下游负责。需要审计时使用任务 ID 回到原 PDF 和资产，而不是把全页坐标送入抽取提示词。

## 持久化

```text
/data
├── jobs.db
└── jobs/<job_id>
    ├── input/original.<ext>
    ├── output/result.md
    ├── output/asset-index.json   # 仅资产下载元数据，不含坐标、正文或表格 IR
    ├── output/assets.zip         # 按需生成
    ├── assets/original/*
    ├── assets/derived/keyframes/*
    ├── assets/derived/previews/*
    └── logs/warnings.json        # 无正文诊断
```

`result.md` 是唯一文档结果。SQLite 是 Job 状态真值，SSE 只是增量体验层。资产索引、
告警和 ZIP manifest 是运维/下载控制数据，不是替代的解析 JSON。

## 安全与边界

上传与 URL 均不可信：校验 magic、OOXML 关系、ZIP 展开边界、FFprobe、大小、单元数、
协议、解析 IP 与每次重定向。模型 URL、模型名和 prompt 只能由服务端配置。
图像、候选正文、prompt 和 reasoning 不进入日志或公共结果。

本项目不负责 Embedding、索引、问答、公司/人物消歧、领域关系与事件编排；它负责把
下游真正需要的可见内容和结构恢复为可高效消费的一份 Markdown 文档。
