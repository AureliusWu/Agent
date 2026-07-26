# 司忆 7.0.0 目录架构收敛执行报告

## 1. 执行结论

- 状态：完成
- 版本：`7.0.0`
- 产品功能：未主动修改
- HTTP API：路由契约保持不变
- 数据库 Schema：保持 28
- 用户数据：未读取、未修改、未迁移
- 本地私有头像：保留且继续由 Git 排除
- 提交：否
- 推送：否

## 2. 实际目录调整

后端已按真实职责收敛到：

- `api/routes`
- `runtime`
- `cognition`
- `context`
- `personality`
- `memory`
- `tools`
- `workspace`
- `providers`
- `extensions`
- `artifacts`
- `security`

`kernel` 与 `evals` 保持原有子包。

以下跨领域模块有意保留在 `app/` 根目录，没有为减少文件数量而强行错误归类：

- `config.py`
- `schemas.py`
- `database.py`
- `data_flow.py`
- `permissions.py`
- `sandbox.py`
- `hooks.py`
- `full_backup.py`
- `diagnostics.py`
- `admin_action_grants.py`
- 桌面生命周期及构建基础模块

前端 34 个组件已分组到：

- `shared`
- `chat`
- `tasks`
- `kokoro`
- `memory`
- `providers`
- `workspace`
- `settings`

56 个后端测试文件已按领域归档；`conftest.py` 保持在 `tests/backend` 根目录。

## 3. 迁移修正

执行过程中发现并修正：

1. `context.py` 与 `context/`、`memory.py` 与 `memory/` 必须原子迁移，不能提前创建同名包。
2. 机械路径替换可能把 `app.providers` 重复写成 `app.providers.providers`，已修正并增加旧路径扫描。
3. 前端资源和 CSS import 迁移后必须保留 `.svg`、`.css` 后缀。
4. 测试目录加深后，5 个依赖 `Path(__file__).parents[]` 的测试路径需要同步增加一级。
5. `sandbox ↔ workspace.worktrees` 和 Kernel/Runtime 既有循环仍被记录；没有通过新增兼容模块掩盖。

## 4. 验证记录

### 基线

- 后端：349 passed，1 个既有 Windows symlink skip
- 覆盖率：82.26%
- 前端 lint：通过
- 前端 build：通过
- 前端安全扫描：通过
- Cargo check：通过

### 分领域

- 叶子领域：68 passed
- 身份/人格：11 passed
- Context：12 passed
- Memory：23 passed
- Tools：47 passed，1 个既有 skip
- Cognition：17 passed
- Runtime：69 passed
- API：43 passed
- 测试路径修复复测：9 passed

### UI

- 宽屏三栏：通过
- 中央夏目心头像：通过
- 右侧夏目心头像与摘要栏：通过
- 前端桌面构建：通过

### 最终门禁

- 后端全量：349 passed，1 个既有 Windows symlink skip
- 前端 lint：通过
- 前端 desktop build：通过，CSS 46.02 kB
- Cargo check：通过
- 本地 Runtime：构建通过，`司忆.exe` 版本 7.0.0
- sidecar smoke：通过
- sidecar build：Release、embedded、DIRTY
- Schema：28/28
- 绑定地址：127.0.0.1
- 真实用户数据：未使用

## 5. 回滚

执行前状态保存在：

```text
build/20260723-structure-refactor-baseline/
```

其中包括：

- tracked 二进制 patch
- 执行前 Git 状态
- 私有头像副本
- 两份本地文档副本

由于用户要求不提交，本次未建立 Git commit；需要回滚时以该备份和 Git rename 状态为依据。

## 6. Git 状态原则

- 移动文件使用 `git mv`，便于 Git 识别 rename。
- 不保留旧路径兼容转发文件。
- 不提交生成物、数据库、日志、密钥或真实用户材料。
- 不提交、不推送。
