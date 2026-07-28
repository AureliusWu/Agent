# 司忆 v9.3.0 本地发布说明

v9.3.0 将 Skill 从提示词文件集合升级为 Skill Runtime 1.0。

- 严格校验名称、语义版本、描述、入口、工具、依赖、权限、平台、风险、许可证及正负触发示例。
- 启动阶段只读取 Manifest 摘要，命中后才读取完整内容，并受单项与总上下文预算约束。
- 依赖按拓扑顺序加载；缺失依赖、循环、同名冲突、未知工具或权限不足均关闭失败。
- 支持内置、工作区与 Extension 来源，支持启停、升级归档和可恢复卸载。
- 内置 release-checklist、data-reliability-audit、performance-regression-check、deploy-smoke-test、daily-report 与 ui-design。
- ui-design 合并既有产品、绿地产品和严格设计系统三种模式。
- 审计界面显示版本、来源、触发原因、依赖链、Token、状态和错误。

仅更新本地源码、可执行文件和安装包；未推送 GitHub，未调用 DeepSeek 付费接口。
