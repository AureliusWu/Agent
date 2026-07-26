# 司忆 7.0.0 目录架构收敛实施方案

> 状态：执行中
> 适用仓库：当前 Git 工作树根目录
> 版本：`7.0.0`
> 原则：只调整物理目录、模块路径和依赖边界，不修改产品功能、HTTP API、数据库 Schema、运行数据路径或 UI 行为。
> 交付方式：仅保留本地修改，不提交、不推送。

## 1. 修订原因

原收敛方案的总体方向合理，但不能原样执行：

1. 预先创建 `app/context/__init__.py` 和 `app/memory/__init__.py` 会遮蔽现有 `context.py`、`memory.py`。
2. `infrastructure` 中规划的部分模块实际依赖记忆、沙箱、MCP、LSP 和信任边界，并非低耦合。
3. 当前测试包含大量字符串 monkeypatch 路径，源码和测试同时迁移容易造成测试失真。
4. 当前存在延迟导入形成的 `sandbox ↔ worktrees` 循环，包初始化顺序变化可能放大问题。
5. 禁止提交的前提下，必须用独立备份、阶段清单和补丁实现回滚。

本方案因此采用“先保护、再测量；先无冲突包、后同名包；先源码、后测试；先叶子、后编排”的顺序。

## 2. 不可变约束

- `VERSION` 保持 `7.0.0`。
- 所有 `/api/*` 路径、请求/响应结构和状态码保持不变。
- SQLite Schema 保持 28。
- `%LOCALAPPDATA%\AureliusWu\Agent` 和 `Agent-Dev` 布局保持不变。
- 不读取、修改或迁移真实用户数据。
- 夏目心身份定义、头像内容、记忆策略、权限规则和模型路由保持不变。
- 本地私有头像不得进入 Git。
- 不新增 `skip`、`xfail`，不放宽断言或安全策略。
- 不保留永久旧路径转发模块。
- 不提交、不推送。

## 3. 受保护的本地状态

执行前已备份到：

```text
build/20260723-structure-refactor-baseline/
```

保护对象：

- `desktop/frontend/src/assets/characters/natsume-kokoro.png`
- `docs/7.0.0/CODEX_RELEASE_HANDOFF_2026-07-22.md`
- `docs/7.0.0/LOCAL_DIRECTORY_ARCHITECTURE.md`
- 当前四个头像/右栏修复文件的二进制 Git patch
- 执行前 Git 状态

## 4. 目标边界

目标不是把所有文件机械放入名称相似的目录，而是保持职责与依赖方向一致。

```text
api
  → runtime
  → cognition / context / personality / memory
  → tools / workspace / providers / extensions
  → security / infrastructure / kernel
```

跨领域基础契约保留在：

- `app/schemas.py`
- `app/kernel/contracts.py`
- `app/kernel/errors.py`

以下模块在职责未能纯化前不强行归入错误领域：

- `database.py`
- `config.py`
- `schemas.py`
- `data_flow.py`
- `permissions.py`
- `full_backup.py`
- `diagnostics.py`
- `admin_action_grants.py`

它们可以暂时保留在 `app/` 根目录；“根目录更少”不能优先于“边界真实”。

## 5. 分阶段执行

### 阶段 0：基线与回滚点

1. 备份当前 tracked diff、未跟踪文档和私有头像。
2. 记录 Git 状态、文件哈希、依赖数量和已知循环。
3. 运行 `scripts/test.ps1`。
4. 运行 Cargo 检查。
5. 记录现有失败；基线失败未解释前不迁移。

### 阶段 1：无同名冲突的叶子领域

按以下顺序逐个迁移，每个领域单独验证：

1. `security`
2. `providers`
3. `workspace`
4. `artifacts`
5. `extensions`

规则：

- 每次只移动一个领域。
- 使用 `git mv` 保留历史识别。
- 更新全部静态 import、字符串 monkeypatch、脚本和文档路径。
- 运行该领域测试和 import smoke。
- 不在本阶段调整业务逻辑。

### 阶段 2：人格与身份

迁移：

- `identity.py`
- `identity_kernel.py`
- `identity_guard.py`
- `affect.py`
- `agent_profiles.py`

保留固定 `agent_id`、身份版本和情绪/关系状态语义。

### 阶段 3：同名包原子迁移

分别执行 `context` 和 `memory`：

1. 在同一个阶段中创建目录；
2. 立即移动同名 `.py`；
3. 立即修复全部 import；
4. 不允许“目录和旧模块并存后再测试”；
5. 运行领域测试和完整后端测试。

`full_backup.py` 仍保留为跨领域服务，除非源码证明确实只处理记忆。

### 阶段 4：工具与工作区边界

迁移工具注册、调度、回执、MCP 和 Skill。

`permissions.py`、`sandbox.py` 和 `hooks.py` 只有在依赖方向明确时才移动；否则保留根目录，避免把跨领域安全边界错误归为工具实现。

先消除或显式记录 `sandbox ↔ worktrees` 延迟循环，不用新增函数内 import 掩盖新循环。

### 阶段 5：认知与 Runtime

先迁移 `cognition`：

- planning
- semantic planner
- reasoning summary
- output protocol

再迁移 `runtime`：

- task lifecycle
- queue
- execution
- verification
- repair
- recovery
- cancellation

`task_runner.py` 最后移动。移动前后分别生成依赖清单，并验证任务创建、队列、停止、恢复、lease、Verifier、工具和多轮执行。

### 阶段 6：API Routes

将 `app/routes` 原子迁移至 `app/api/routes`，同步修改 `main.py` 和测试 monkeypatch。

验收重点：

- 路由表完全一致；
- HTTP 方法和状态码一致；
- Token 中间件仍生效；
- Route 不新增数据库、Prompt 或权限业务逻辑。

### 阶段 7：后端测试目录

源码稳定且全量测试通过后，再按领域移动测试。

- `conftest.py` 保持在 `tests/backend/`。
- 跨领域测试放入 `integration/`。
- 不修改断言语义。
- 不通过兼容导出维持旧 monkeypatch。

### 阶段 8：前端组件目录

按 `shared/chat/tasks/kokoro/memory/providers/workspace/settings` 分组。

- 只移动 `.tsx` 并修复 import。
- Props、CSS class、状态和 DOM 行为不变。
- 保护本地夏目心头像。
- 验证宽屏三栏、窄屏右抽屉、中央头像和右栏头像。

### 阶段 9：文档、打包与最终验证

更新当前架构和目录文档，不篡改历史报告。

最终执行：

1. 后端全量测试；
2. 前端 lint、build、安全扫描；
3. Cargo test/check；
4. sidecar 构建与隔离 smoke；
5. 本地 Tauri Runtime 构建；
6. 桌面 UI smoke；
7. release metadata、隐私与路径审计。

30 分钟至 24 小时耐久门禁不属于本次纯目录重构的完成条件。

## 6. 阶段验收模板

每阶段记录：

```text
领域：
移动文件：
新增 __init__.py：
修改 import：
修改测试：
静态旧路径扫描：
测试命令：
测试结果：
已知风险：
回滚来源：
```

阶段失败时：

1. 停止进入下一领域；
2. 修复当前阶段；
3. 若无法保持行为不变，从基线补丁/备份恢复；
4. 不堆积多个失败领域。

## 7. 完成定义

- 目标领域目录已建立，职责与真实依赖一致。
- 无同名模块遮蔽。
- 无遗留旧 import 和旧 monkeypatch 路径。
- 无新增循环依赖。
- HTTP 路由、Schema、运行数据路径和 UI 行为不变。
- 后端、前端、Cargo、sidecar 和桌面 smoke 通过。
- 原头像和右侧栏修复仍然有效。
- 本地私有文件未进入 Git。
- 未读取或修改真实用户数据。
- 未提交、未推送。
