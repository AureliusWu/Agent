# 司忆 v9.5.0 本地发布报告

状态：`READY / LOCAL_ONLY`。

实现提交：`d0568b54ccb7f732f1d650a3147eeeaa3e1d0c41`。

## 门禁

| 门禁 | 状态 | 证据 |
|---|---|---|
| 上下文、队列与恢复专项 | PASS | `77 passed` |
| 后端完整回归 | PASS | `519 passed, 7 skipped` |
| 真实 Ollama qwen3:4b | PASS | `5/5`，耗时 `875.89 s` |
| Core Eval | PASS | `18/18`，run id `3b212b5cfbf34ed598a0f0d5edf47495` |
| Rust | PASS | `6/6` |
| 前端 | PASS | lint、安全契约、TypeScript、Vite build |
| 隐私 | PASS | tracked + staged + history |
| 性能 | PASS | `1684 / 1654 / 1667 ms`，中位数 `1667 ms` |
| 安装与数据保留 | PASS | NSIS、MSI、隔离启动、v34→v35、v9.4.0 升级、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 未授权付费调用 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 分发

仅本地源码、可执行文件和安装包更新；未推送 GitHub。
