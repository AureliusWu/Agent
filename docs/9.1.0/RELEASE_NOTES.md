# 司忆 v9.1.0 本地发布说明

- 统一 DeepSeek/OpenAI-compatible、Ollama 与 Mock 的 Provider 能力接口。
- 新增 `stream()`、`vision()`、`embedding()`、`list_models()`、`capabilities()` 与 `estimate_context()` 契约。
- 不支持的能力统一返回 `unsupported_capability`，不再静默降级或伪装成功。
- 通过 Ollama `/api/show` 真实读取 qwen3:4b 的上下文窗口和模型能力。
- Provider 切换界面新增身份、长期记忆、会话连续性与能力兼容提示。
- 本次仅本地发布，不推送 GitHub；DeepSeek 付费实测未执行。
