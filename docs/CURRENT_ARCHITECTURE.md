# Current Architecture Baseline

## Runtime

司忆 `2.0.0` 以 Tauri 2 Windows 桌面壳启动 FastAPI sidecar。React 前端通过本机受令牌保护的 HTTP API 使用对话、任务、文件、Skill、MCP 和设置能力。`backend/app/task_runtime.py` 负责持久任务，`task_runner.py` 负责规划、模型循环、工具调用、暂停恢复与验证，`provider.py` 提供 OpenAI-compatible/DeepSeek 调用。

## Persistence

SQLite schema v16 已保存对话、消息、任务、模型用量、工具执行、审批、检查点、验证、审计、多 Agent、Provider 能力和 `workspace_memories`。迁移前会备份，失败会恢复。当前完整备份只支持数据库文件，不包含身份、设置和校验清单。

## Context And Token Flow

`context.py` 保存结构化会话摘要并提供手动压缩；`efficiency.py` 提供任务自适应预算、单次输出限制、调用前输入估算和工具结果压缩；`provider_capabilities.py` 保存观测能力。当前尚未按具体模型上下文窗口计算完整请求预算，也不会在每次调用前自动触发分层压缩。

## Memory And Identity

`memory.py` 已区分项目和个人记忆，支持可信度、分类、检索、反馈、导入和导出。其数据模型仍以工程键值记忆为中心，缺少唯一 `agent_id`、身份版本、标准长期记忆类型、锁定/敏感/时间有效性/替代关系、情绪关系状态和候选审核链路。夏目心身份目前主要来自 Agent Profile system prompt，不是独立且版本化的 Identity Kernel。

## Desktop UI

当前 UI 已有对话、项目、搜索、记忆、用量、文件、扩展、审计和设置二级入口。聊天支持流式推理、停止/暂停/恢复、权限模式和自动编排。记忆页支持个人/项目记忆与导入导出，但缺少来源历史、锁定、确认、冲突和状态页。

## Protected Existing Work

开始 v2.0.1 时工作区包含上一轮尚未提交的 Token、推理、项目、用量和记忆改动，以及用户已有的 `docs/V2_DESKTOP_MVP_HANDOFF.md` 修改。本轮在本地分支 `codex/v2.0.1` 上承接前者，并不覆盖后者。
