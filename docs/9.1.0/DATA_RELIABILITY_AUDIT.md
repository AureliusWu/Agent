# v9.1.0 Provider 能力数据可靠性审计

结论：`PASS`。

- qwen3:4b 的 `context_window=262144` 来自本机 Ollama v0.32.5 `/api/show` 的 `qwen3.context_length`。
- tools 与 reasoning 仅在 Ollama 返回 `capabilities=["completion","tools","thinking"]` 后标记为支持。
- vision 与 embeddings 在没有实际证据时标记为不支持，不由模型名称或 embedding length 推断。
- 未知上下文不会以默认数字伪装成已探测值；界面显示“待实际探测”。
- 不支持的能力抛出 `unsupported_capability`，不会返回空数组、空对象或伪造 PASS。
- Provider 健康结果和能力来源一并返回；模型元数据只用于能力展示与预算，不写入身份或长期记忆。
- DeepSeek 付费实测未执行，相关状态保持 `NOT_RUN`。
