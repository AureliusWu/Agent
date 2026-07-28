# 司忆 v9.0.0 本地模型测试体系实施报告

## 结果摘要

本次升级已完成统一 Provider 架构、确定性 Mock 测试体系、Ollama 本机安全边界、
DeepSeek 付费测试硬门禁、桌面 Provider 配置与诊断，以及分层执行脚本。

实际执行没有产生 DeepSeek 付费调用，成本为 **0 美元**。A 层 Mock 全部 PASS；
Ollama B 层因当前环境无法完成 1.56 GB 官方安装包下载而如实标记 BLOCKED；
DeepSeek C 层因用户要求节省 token 且未启用双授权开关而标记 NOT_RUN，门禁本身 PASS。
没有使用模拟结果、代码审查或推断替代 B/C 层实际运行。

## 已实现内容

- `LLMProvider` 统一契约：
  `chat`、`stream_chat`、`tool_call`、`structured_output`、`cancel`、
  `health_check`、`get_capabilities`、`count_tokens`。
- DeepSeek 兼容 Provider：保留原传输、能力观测、数据流审计和旧任务快照兼容。
- Mock Provider：17 类确定性正常、错误、超时、取消、协议和工具调用场景。
- Ollama Provider：
  - 只允许本机回环地址；
  - 只允许 11434 端口；
  - 模型固定 `qwen3:4b`；
  - 不允许隐式回退到云端或其他模型。
- 六类统一失败归因：
  `MODEL_FAILURE`、`PROTOCOL_FAILURE`、`RUNTIME_FAILURE`、`TOOL_FAILURE`、
  `VERIFICATION_FAILURE`、`ENVIRONMENT_FAILURE`。
- 付费测试必须同时设置：
  `SIYI_TEST_PROVIDER=deepseek` 与 `SIYI_ALLOW_PAID_API=true`。
- 桌面设置增加 Provider、超时、流式和工具调用配置；密钥仍仅保存在
  Windows Credential Manager，不写入 JSON 配置。
- 提供检测、安装、模型拉取和 A/B/C 三层测试脚本。

## 实际证据

| 门禁 | 状态 | 证据 |
|---|---|---|
| A 层 Mock | PASS | `40 passed`，17 类场景逐类执行 |
| Provider/API 聚焦回归 | PASS | `91 passed, 4 skipped` |
| 后端全量回归 | PASS | `452 passed, 5 skipped`，覆盖率高于 `82%` |
| Core Eval | PASS | `18/18` |
| Adversarial Eval | PASS | `4/4` |
| Rust | PASS | `5/5` |
| 前端 lint/build | PASS | `oxlint`、TypeScript、Vite production build |
| 隐私扫描 | PASS | tracked + history 未发现禁入私密数据 |
| Sidecar / 安装包烟雾 | PASS | 隔离数据目录、NSIS 与 MSI 构建/启动/清理通过 |
| B 层 Ollama | BLOCKED | 未安装；官方安装包下载受当前网络环境阻塞 |
| C 层 DeepSeek | NOT_RUN | 双开关未授权，脚本在请求前 BLOCKED |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

机器可读明细见 `TEST_MATRIX.json`。

## 本地产物

- `司忆.exe`：ProductVersion `9.0.0`，SHA256
  `F2510BC4B4D42D2160C72A9C11508EB3DCD75E9E9442D3156B1CAFC67CDC9549`
- `司忆_9.0.0_x64-setup.exe`：SHA256
  `15745C8F000AC4F19F0E8F18C7EB5A8AF2D5D8DF470123B3DFE611BC276A1232`
- `司忆_9.0.0_x64_zh-CN.msi`：SHA256
  `6FCF9FC39EF48139AD43F15C9118E61344EC42E5B0AFDB222AE00478713410C5`

## Ollama 环境证据

- 官方 Windows 文档确认默认本机 API 为 `http://localhost:11434`。
- Windows Package Manager 清单：
  - Package：`Ollama.Ollama`
  - Publisher：`Ollama`
  - Version：`0.32.5`
  - Installer SHA256：
    `b7eeef038ddcbd09ac665b11872baff1bc9b42794be41b5ef187b2f4b16a4498`
- 官方安装包 Content-Length：`1563078600` bytes。
- 官方模型目录确认 `qwen3:4b`，摘要前缀 `359d7dd4bcda`，约 2.5 GB。
- 本机检测结果：Ollama 未安装、服务未运行、`qwen3:4b` 未安装，退出码 2。

## 兼容与安全

- Runtime 已统一通过 Registry 获取 Provider。
- DeepSeek 的持久化 profile 保持与旧任务一致，中断任务不会因 v9 适配层发生
  profile 漂移。
- Ollama 的本地 HTTP 例外只在受限 Provider 内显式启用，公共 HTTP 和私网地址
  仍由原网络安全策略拒绝。
- Provider 配置使用原子替换，不包含 API Key。
- 旧专业/多 Agent 请求模式自 v8.0.3 起按产品边界映射为单一基础 Agent；
  对应旧 Eval 标记 NOT_APPLICABLE，没有为了测试重新启用已收敛功能。

## 回滚

代码基线为 `6f1e27a`。如需回退运行配置，删除运行数据目录下
`state/provider-settings.json` 即恢复 DeepSeek 默认配置。Ollama 未成功安装，
因此本次没有需要卸载的 Ollama 程序或模型。
