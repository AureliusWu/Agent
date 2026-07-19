# 7.0.0 源码布局迁移报告

状态：完成并通过完整测试。

## 迁移结果

- 通用 Agent：`backend/` → `siyi/`。
- 身份公开定义：`backend/identity/` → `kokoro/identity/`。
- 前端：`frontend/` → `desktop/frontend/`。
- Tauri：`frontend/src-tauri/` → `desktop/src-tauri/`。
- 公开资源：`frontend/public/` → `desktop/public/`。
- 测试与 Eval：`tests/backend/`、根 `evals/`。
- 新增边界目录：`contracts/`、`extensions/`、`resources/`、`packaging/`。

所有活动构建、CI、发布、验收和文档路径已同步。旧 `backend/data` 仅保留在旧数据迁移兼容测试中。

提交：`1981bcb refactor: migrate source layout`。

## 验证

- 后端：347 passed，1 skipped。
- 前端 lint/build/security/build-info：通过。
- Rust：4 passed。
- 核心 Eval：18/18。
- 工作区、跟踪文件和暂存区隐私扫描：通过。
