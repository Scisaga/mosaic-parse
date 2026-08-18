# MosaicParse 项目规格

唯一维护中的项目规格见 [docs/project-spec.md](docs/project-spec.md)。

当前架构结论：本项目以 `ContentParseResult` 为主产物，Markdown / Plain Text
仅为派生视图；实体、关系、`ReportedFact` 和事件抽取由 EventRail 等下游系统负责。
