# 司忆 v13.0.0 本地发布说明

v13.0.0 聚焦专业任务执行与性能：恢复真实多 Agent 编排和专业 Agent 配置，新增任务需求、验收条件、依赖图及预算持久化，落地 Planner、Executor、Reviewer、Verifier、Recovery Coordinator 的结构化协作边界，并提供性能与 Provider 策略审计视图。

本版明确禁止本地模型静默回退到付费 Provider。Ollama `qwen3:4b` 已完成 15 项真实能力测试；协作评测 10/10、专业任务评测 8/8、核心评测 18/18、对抗评测 4/4。

最终本地发布状态以同目录 `RELEASE_STATUS.json` 和 `TEST_MATRIX.json` 为准。
