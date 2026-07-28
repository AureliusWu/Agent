# 司忆 v9.2.0 本地发布报告

## 结论

v9.2.0 已完成核心文件能力统一、真实 Windows 文件系统验收、完整回归、桌面封包和隔离安装烟测，状态为 `READY / LOCAL_ONLY`。实现绑定提交 `1144fd8ce844568d2417db28cb08597cf84e86f9`。

## 门禁

| 门禁 | 状态 | 证据 |
|---|---|---|
| 文件/API 专项 | PASS | `77 passed, 1 skipped` |
| 后端完整回归 | PASS | `499 passed, 1 skipped` |
| 覆盖率 | PASS | `82.81%` |
| Core Eval | PASS | `18/18`，run id `36be559131154a4892aad61d353765af` |
| Rust | PASS | `5/5` |
| 前端 | PASS | lint、安全契约、TypeScript、Vite desktop build |
| 隐私 | PASS | tracked + history |
| 性能 | PASS | 最终样本见 `build/v920-evidence/performance-gate-final.json` |
| 安装与数据保留 | PASS | NSIS、MSI、隔离启动、卸载保留、重装识别 |
| DeepSeek 付费 | NOT_RUN | 未授权付费调用 |
| 24 小时耐久 | NOT_APPLICABLE | 用户明确排除 |

## 能力结果

统一逻辑层提供 `file.read/write/patch/copy/move/rename/delete/restore/search/list/stat` 与 `directory.create/list/move/delete`。旧下划线模型工具名继续作为兼容适配器。所有变更可以 dry-run；目录删除先备份且受条目上限保护；批量最多 50 项，中途失败会恢复本批已完成变更。

## 分发

仅本地源码、可执行文件和安装包更新；未推送 GitHub。
