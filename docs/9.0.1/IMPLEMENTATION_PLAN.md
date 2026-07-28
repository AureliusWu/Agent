# 司忆 v9.0.1 实施计划

## 版本目标

将 v9.0.0 的 Ollama + `qwen3:4b` 从“真实测试可用”提升为“日常稳定可用”。本版本只处理本地模型稳定性、诊断、指标和设置页体验，不引入 v9.1.0 之后的大型架构。

基线：

- 版本：`9.0.0`
- 分支：`local/v9.0.0`
- 基线 HEAD：`e57296689ffc311fc4a7a35456f7e4d6e69b8acc`
- 本地模型：Ollama `0.32.5` + `qwen3:4b`
- 发布方式：仅本地，不推送 GitHub

## 代码审计结论

### 已存在并保留

- Ollama 仅允许回环地址和 `11434` 端口。
- 模型固定为 `qwen3:4b`，不会自动下载或回退到付费模型。
- OpenAI-compatible 的 `content`、`reasoning`、`reasoning_content` 和流式 `tool_calls` 已有统一解析基础。
- Provider 配置已有超时、最大输出、流式与工具调用开关。
- 运行时取消会直接取消正在等待的模型任务。
- Provider 传输已有有限重试和错误分类基础。
- Provider、模型、总耗时、Token 和重试次数已写入 `model_runs`。
- API Key 使用 Windows Credential Manager，不写入 Provider JSON。

### 本版本缺口

- 设置页未显示 Ollama 服务详情与实际安装模型列表。
- Ollama 服务未启动、模型缺失、`11434` 非 Ollama 服务时的错误仍不够可操作。
- 首 Token 时间未记录。
- 首次模型加载没有明确提示。
- 小输出预算仍可能使 Qwen3 只返回思考、正文为空。
- 本地 Provider 的重试次数不能独立配置。
- 缺少 10 轮普通对话、10 次流式、10 次工具调用和 3 秒取消的真实稳定性验收。
- 缺少服务停止、模型缺失和端口协议错误的稳定错误契约测试。

## 实施范围

### 后端

1. 为 Ollama 增加只读模型列表和诊断结果：
   - 服务状态；
   - Ollama 版本；
   - 已安装模型名称、大小和修改时间；
   - 当前模型是否存在；
   - 首次加载提示。
2. 增加可操作错误：
   - `ollama_service_unavailable`；
   - `ollama_model_missing`；
   - `ollama_port_conflict`；
   - `ollama_invalid_response`。
3. 对 `qwen3:4b` 应用经 10×真实稳定性验证的最小输出预算。初始候选 `512` 已证实不足，当前按失败证据提高到 `2048`，并在配置层拒绝更低的 Ollama 上限。
4. Provider 配置增加 `max_retries`，Ollama 调用使用该值。
5. 流式请求记录首个正文、思考或工具增量到达时间 `first_token_ms`。
6. `model_runs` 增加 `first_token_ms`，迁移必须兼容旧数据库。
7. Provider 健康、配置和诊断 API 不返回凭据或完整敏感端点。

### 桌面设置页

1. Provider 为 Ollama 时展示：
   - 服务可用/不可用；
   - 当前模型；
   - 实际安装模型列表；
   - 首次加载提示；
   - 可操作错误建议。
2. 模型选择仅允许实际安装且受批准的 `qwen3:4b`。
3. 模型缺失时不提供自动下载按钮。
4. 增加本地模型重试次数配置。
5. 保持 DeepSeek 密钥区域只在 DeepSeek 模式显示。

## 测试计划

### 单元与集成

- 模型列表正常、模型缺失、服务不可用、端口协议冲突。
- Ollama 最小输出预算。
- `reasoning`、`content`、流式正文、流式思考和流式工具调用。
- `first_token_ms` 数据库迁移、写入、API 展示。
- `max_retries` 配置校验和传递。
- 日志、配置、API 响应中不出现 API Key。

### 真实 Ollama

- 普通对话连续 10 轮；
- 流式输出连续 10 次，正文均非空；
- 工具调用连续 10 次，无协议错误；
- 正在运行的本地模型请求取消在 3 秒内完成；
- 不下载其他模型；
- 不调用 DeepSeek。

### 发布门禁

- v9.0.1 专项测试；
- `scripts/test.ps1`；
- 相关固定 Eval；
- `cargo test --locked`；
- 隐私扫描；
- `scripts/build-desktop.ps1`；
- Sidecar、NSIS、MSI、升级、卸载重装烟测；
- 版本源一致性、产物 SHA256、工作区干净。

## 参考 Skill 处理规则

`skills.zip` 仅作为流程与能力边界参考，不直接安装或执行。本版本不复制其中任何脚本。

- `release-checklist`、`data-reliability-audit`、`performance-regression-check`、`deploy-smoke-test`：后续作为内置 Skill 的行为参考。
- `frontend-design` 与 `design-skill-v2`：后续 v9.3.0 clean-room 合并为 `ui-design`。
- DOCX、PDF、PPTX Skill 标注专有许可：后续 v9.7.0 只能依据公开格式标准和公开依赖 clean-room 实现，不复制提示词、脚本或 Schema。
- `skill-creator`、`find-skills`、`mcp-builder`：后续只进入管理员/开发者域。

## 风险与回滚

- 数据库新增字段：构建前必须验证旧数据库迁移与失败恢复。
- Ollama 真实稳定性测试耗时较长：保留逐项证据，不以较短测试替代。
- 取消测试必须只影响测试任务，不停止 Ollama 服务。
- 回滚点：`e57296689ffc311fc4a7a35456f7e4d6e69b8acc`。
- 本版本不推送 GitHub。
