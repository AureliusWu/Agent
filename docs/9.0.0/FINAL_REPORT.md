# 司忆 v9.0.0 本地模型测试体系实施报告

## 结果摘要

本次升级已完成统一 Provider 架构、确定性 Mock 测试体系、Ollama 本机安全边界、DeepSeek 付费测试硬门禁、桌面 Provider 配置与诊断，以及分层执行脚本。

Ollama `0.32.5` 与 `qwen3:4b` 已在本机真实接入。健康检查、普通对话、流式输出和含工具调用的多轮流程全部通过，没有使用模拟结果、代码审查或推断代替实际运行。DeepSeek 付费验收继续按用户的 token 节省要求标记 `NOT_RUN`，双开关门禁本身通过，本次付费 API 成本为 `0 美元`。

v9.0.0 已完成本地构建与安装包烟测，没有推送 GitHub。

## 已实现内容

- 统一 `LLMProvider` 套约：`chat`、`stream_chat`、`tool_call`、`structured_output`、`cancel`、`health_check`、`get_capabilities`、`count_tokens`。
- DeepSeek 兼容 Provider 保留原传输、能力观测、数据流审计和旧任务快照兼容。
- Mock Provider 覆盖确定性正常、错误、超时、取消、协议和工具调用场景。
- Ollama Provider：
  - 只允许本机回环地址；
  - 只允许 `11434` 端口；
  - 模型固定为 `qwen3:4b`；
  - 不允许隐式回退到云端或其他模型；
  - 兼容 Ollama OpenAI 接口的 `reasoning` 字段，并保持私有推理不进入公开响应。
- 付费测试必须同时设置 `SIYI_TEST_PROVIDER=deepseek` 与 `SIYI_ALLOW_PAID_API=true`。
- 桌面设置支持 Provider、超时、流式和工具调用配置；密钥仍只保存在 Windows Credential Manager。

## 实际证据

| 门禁 | 状态 | 证据 |
|---|---|---|
| A 层 Mock | PASS | `40 passed` |
| B 层 Ollama | PASS | Ollama `0.32.5`、`qwen3:4b`；`3 passed in 15.98s` |
| B 层健康检查 | PASS | installed/service/model 均为 `true`，退出码 `0` |
| B 层普通对话 | PASS | 真实返回 `LOCAL_OK` |
| B 层流式输出 | PASS | 收到真实 delta 并返回 `STREAM_OK` |
| B 层多轮工具调用 | PASS | 原生调用 `lookup_temperature`，工具结果 `28` 被用于最终回答 |
| 后端全量回归 | PASS | `453 passed, 5 skipped`，覆盖率 `82.52%` |
| Core Eval | PASS | `18/18` |
| Adversarial Eval | PASS | `18/18` |
| Rust | PASS | `5/5` |
| 前端 lint/build/security | PASS | oxlint、TypeScript、Vite、前端安全契约全部通过 |
| 隐私扫描 | PASS | tracked + history 未发现禁入私密数据 |
| Sidecar / 安装包烟测 | PASS | 隔离数据目录、NSIS、MSI、启动、卸载与重装验证通过 |
| C 层 DeepSeek | NOT_RUN | 用户要求节省 token；双开关未授权，请求前即停止 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

机器可读明细见 `TEST_MATRIX.json`。

## 本地产物

- `司忆.exe`：ProductVersion `9.0.0`；SHA256 `AC1119828117FE3B64D772EC15FA40C14F1E0E498E9FAA907747C32370D5A754`
- `司忆_9.0.0_x64-setup.exe`：SHA256 `5BBE5B977F7B58BBCEDB320054D3E75DD2C95B99EE0EBF86D0AB604B7FA86845`
- `司忆_9.0.0_x64_zh-CN.msi`：SHA256 `DF408D9D1D88AD1685E30CC35D6658682D8BF074F9F962F1275370B5B013E73F`

产物绑定源码提交 `a5ca57c3faabc8f47203f2c5d7eb4d3d2582a77f`。

## 发布结论

本地 v9.0.0 已完成，状态为 `READY / LOCAL_ONLY`。付费 DeepSeek 验收不属于本次授权执行范围，保持 `NOT_RUN`，没有伪造 PASS。未执行 GitHub 推送或远程分发。

## 回滚

运行配置回滚可删除运行数据目录下的 `state/provider-settings.json`，恢复默认 DeepSeek 配置。源码基线可回退到 `3ad5ab3`；本机 Ollama 与 `qwen3:4b` 是用户授权安装的独立运行环境，回滚应用代码不会自动卸载它们。
