# 司忆 v9.3.0 本地发布报告

状态：`READY / LOCAL_ONLY`。

实现提交：`4bd90e5325d09066aecceef006a382b739730d07`。

## 门禁

| 门禁 | 状态 | 证据 |
|---|---|---|
| Skill Runtime 专项 | PASS | `29 passed` |
| 后端完整回归 | PASS | `507 passed, 1 skipped, 6 deselected` |
| 真实 Ollama qwen3:4b | PASS | `5/5` |
| Core Eval | PASS | `18/18`，run id `5148fb4940a74c109dd87adcdc5f2d73` |
| Rust | PASS | `5/5` |
| 前端 | PASS | lint、安全契约、Build Info、TypeScript、Vite desktop build |
| 隐私 | PASS | tracked + history |
| 性能 | PASS | 预提交 `1642 / 1647 / 1653 ms`，最终样本见忽略的 build evidence |
| 安装与数据保留 | PASS | NSIS、MSI、隔离启动、v33→v34 迁移、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 未授权付费调用 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 能力结果

Skill Runtime 1.0 已实现严格 Manifest、正负触发、启动摘要发现、完整内容延迟加载、依赖拓扑与循环检测、同名冲突闭锁、工具权限预检、版本归档、可恢复卸载、触发审计和 Token 统计。失败不会注入部分 Skill 内容，也不会污染主对话。

## 分发

仅本地源码、可执行文件和安装包更新；未推送 GitHub。
