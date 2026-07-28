# 司忆 v9.1.0 本地发布报告

## 结论

v9.1.0 已完成统一 Provider 能力层、真实 Ollama 验收、完整回归、桌面封包和隔离安装烟测，发布状态为 `READY / LOCAL_ONLY`。实现代码绑定提交 `a963c09a70a1ec2c401323198d85a37dcc61bf5a`。

## 实际验证

| 门禁 | 状态 | 证据 |
|---|---|---|
| Provider 统一契约 | PASS | Mock、隔离 Ollama、无网络远程传输契约通过 |
| Provider 切换身份连续性 | PASS | `18/18` |
| 后端完整回归 | PASS | `489 passed, 1 skipped` |
| 覆盖率 | PASS | `82.84%`，高于 70% 门槛 |
| 真实 qwen3:4b | PASS | `5 passed in 279.96s`，实际调用 31 次 |
| Ollama 能力探测 | PASS | Ollama v0.32.5；qwen3:4b 上下文窗口 `262144`；tools/reasoning 真实可用 |
| Core Eval | PASS | `18/18`，run id `924ea31beb7346fea45b1f3916a35f03` |
| Rust | PASS | `5/5` |
| 前端 | PASS | lint、安全契约、TypeScript、Vite desktop build |
| 隐私 | PASS | tracked + history 均无禁入私密数据 |
| 性能 | PASS | 最终清洁封包样本见 `build/v910-evidence/performance-gate-final.json` |
| 安装与数据保留 | PASS | NSIS、MSI、隔离启动、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 用户要求节省 Token，付费调用为 0 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 兼容性与数据语义

不支持的工具、vision、embedding、JSON 或 streaming 能力会明确返回 `unsupported_capability`，不会用空结果代替成功。qwen3:4b 的上下文窗口来自本机 Ollama `/api/show` 实际响应，不使用模型名称猜测。Provider 切换不修改身份、长期记忆、会话或暂停任务使用的稳定 Provider 快照。

## 分发

本次只更新本地源码、`司忆.exe`、sidecar、NSIS 和 MSI；没有推送 GitHub，也没有远程分发。
