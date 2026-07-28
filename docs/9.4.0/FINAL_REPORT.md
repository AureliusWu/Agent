# 司忆 v9.4.0 本地发布报告

状态：`READY / LOCAL_ONLY`。

实现提交：`85d67b27ea198691b59718bedc22eff936f343e2`。

## 门禁

| 门禁 | 状态 | 证据 |
|---|---|---|
| 权限与安全专项 | PASS | `121 passed, 1 skipped` |
| 后端完整回归 | PASS | `516 passed, 1 skipped, 6 deselected` |
| 真实 Ollama qwen3:4b | PASS | `5/5` |
| Core Eval | PASS | `18/18`，run id `3c828b4d9e7b402f91086a4f7e887eca` |
| Rust/Credential Manager | PASS | `6/6` |
| 前端 | PASS | lint、安全契约、TypeScript、Vite build |
| 隐私 | PASS | tracked + staged + history |
| 性能 | PASS | 预提交 `1654 / 1661 / 1648 ms`，最终样本见忽略的 build evidence |
| 安装与数据保留 | PASS | NSIS、MSI、隔离启动、v34→v35 迁移、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 未授权付费调用 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 安全结果

Permission Broker v2、持久 allow/deny 与撤销、安全域、Skill 隔离扫描、命令允许列表、运行时依赖安装禁令、域名策略和 opaque Secret 引用均已接入。critical 权限不能持久允许，管理员域不能绕过核心确认与沙箱。

## 分发

仅本地源码、可执行文件和安装包更新；未推送 GitHub。
