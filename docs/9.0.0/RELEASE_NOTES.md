# 司忆 v9.0.0 本地版本说明

## 主要更新

- 新增统一 `LLMProvider` 与 Provider Registry，保留 DeepSeek 兼容行为。
- 新增确定性 Mock Provider 测试层及无网络回归。
- 新增仅允许本机 `qwen3:4b` 的 Ollama Provider、安全边界和真实验收层。
- 兼容 Ollama OpenAI 接口的独立 `reasoning` 输出字段，私有推理不会作为公开正文展示。
- 新增 DeepSeek 付费测试双开关硬门禁，默认测试不消耗 token。
- 新增统一失败归因、Provider 健康检查和能力声明。
- 桌面设置支持 DeepSeek、Ollama、Mock 切换，以及超时、流式和工具调用配置。
- 新增 Ollama 检测、安装/模型拉取及 A/B/C 三层测试脚本。

## 验证状态

Ollama `0.32.5` 与 `qwen3:4b` 已真实安装并运行，健康检查、普通对话、流式输出和多轮工具调用全部通过。Mock、后端全量回归、Core Eval、Adversarial Eval、前端、Rust、隐私、Sidecar 和安装包烟测均通过。

DeepSeek 付费验收按用户节省 token 的要求保持 `NOT_RUN`；请求前双开关门禁已验证有效，本次没有产生付费模型调用。

本版本仅在本地更新、提交和构建，没有推送 GitHub。
