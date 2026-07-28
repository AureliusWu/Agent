# 司忆 v9.1.0 实施计划

## 目标

在不改变身份、会话、任务恢复和现有 Provider 配置语义的前提下，把 DeepSeek/OpenAI-compatible、Ollama 与 Mock 收敛到同一能力契约。能力不支持时必须明确拒绝，不能以空结果或伪成功代替。

## 现状审计

- 已有统一 `LLMProvider`，覆盖聊天、流式、工具调用、结构化输出、取消、健康检查、Token 粗估与 Provider Profile。
- 已有 Provider 错误分类、运行观测、能力缓存和 Ollama 真实诊断。
- 已有 reasoning 私有字段与公开摘要分离，不能另建第二条解析路径。
- 缺口是 vision、embedding、统一 `stream()` 命名、模型列表、上下文估算、模型级输出预算，以及“不支持能力”的标准拒绝。
- 当前远程传输层本身兼容 OpenAI-compatible API；本版本不新增凭据来源、不发起付费验收调用。

## 实施范围

1. 扩展统一能力描述：
   - stream/tools/vision/reasoning/json mode/embeddings；
   - context window；
   - default max output tokens；
   - capability source。
2. 扩展 `LLMProvider` 契约：
   - `stream()`；
   - `vision()`；
   - `embedding()`；
   - `list_models()`；
   - `capabilities()`；
   - `estimate_context()`。
3. 默认实现对不支持的能力返回稳定的 `unsupported_capability`，并归入环境/配置类失败，不伪装成功。
4. 让 DeepSeek/OpenAI-compatible、Ollama、Mock 都实现同一契约；保留旧方法作为兼容入口。
5. 将健康检查、模型列表与运行观测合并成可追踪的能力探测结果。
6. Provider 切换时返回兼容性提示，但不修改身份、长期记忆或会话数据。
7. 前端展示上下文窗口、默认输出预算和新增能力状态。

## 明确不做

- 不调用 DeepSeek 付费 API。
- 不新增或下载其他模型。
- 不把 vision/embedding 标记为可用，除非对应 Provider 有实际证据。
- 不改变夏目心身份内核、长期记忆所有权或现有会话格式。
- 不推送 GitHub。

## 验收

- 统一契约测试在 Mock 与真实 Ollama 上运行；远程 Provider 只做无网络单元/传输契约测试，付费真实调用标记 `NOT_RUN`。
- 不支持的工具、vision、embedding 和 JSON 能力均有明确拒绝测试。
- Provider 切换后身份连续性 `18/18 PASS`。
- reasoning 私有内容不进入公开响应、日志或证据。
- 专项测试、后端全量、前端、Rust、Core Eval、隐私、性能、桌面封包和隔离安装烟测全部通过后，才把版本源更新为 v9.1.0。

## 回滚

本版本从 v9.0.1 本地打包提交 `8ffc5606510c66a5995f656141b26485a3a205eb` 开始。若统一契约破坏现有 Provider 行为，回滚本版本提交即可；Provider 配置与 Schema 33 不需要降级。
