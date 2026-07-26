# 司忆 7.0.0 本地目录架构

> 收敛后快照：2026-07-23
> 仓库：当前 Git 工作树根目录
> 分支：`codex/v2.0.1`
> 版本：`7.0.0`

## 1. 总体结构

```text
Agent\
├─ .github\                 CI、评估与发布工作流
├─ contracts\               跨组件契约
├─ desktop\                 React 前端与 Tauri Windows 壳
├─ docs\                    架构、版本、验收和交接文档
├─ evals\                   固定评估任务与门禁
├─ examples\                扩展示例
├─ extensions\              扩展系统说明
├─ kokoro\                  夏目心固定身份定义
├─ scripts\                 开发、测试、构建、迁移和发布脚本
├─ siyi\                    FastAPI sidecar 与 Agent 核心
├─ tests\                   按领域组织的自动化测试
├─ build\                   本地生成物与回滚备份（Git 忽略）
├─ VERSION
├─ 司忆.exe
└─ agent-backend.exe
```

运行链路：

```text
司忆.exe（Tauri）
├─ desktop/frontend/dist
├─ agent-backend.exe
└─ 仅本机令牌访问 FastAPI
   ├─ API routes
   ├─ Runtime 编排
   ├─ cognition / context / personality / memory
   ├─ tools / workspace / providers / extensions
   └─ security / kernel / 基础设施
```

## 2. 后端领域结构

```text
siyi\
├─ run_server.py
├─ pyproject.toml
├─ requirements.lock
├─ uv.lock
└─ app\
   ├─ __init__.py
   ├─ main.py
   ├─ config.py
   ├─ schemas.py
   ├─ database.py
   ├─ admin_action_grants.py
   ├─ build_info.py
   ├─ data_flow.py
   ├─ deployment.py
   ├─ desktop_lifecycle.py
   ├─ diagnostics.py
   ├─ efficiency.py
   ├─ environment.py
   ├─ full_backup.py
   ├─ hooks.py
   ├─ logging_config.py
   ├─ permissions.py
   ├─ process_supervisor.py
   ├─ runtime_paths.py
   ├─ sandbox.py
   ├─ api\
   │  ├─ __init__.py
   │  └─ routes\
   │     ├─ __init__.py
   │     ├─ agents.py
   │     ├─ backups.py
   │     ├─ chat.py
   │     ├─ conversations.py
   │     ├─ extensions.py
   │     ├─ identity.py
   │     ├─ long_term_memories.py
   │     ├─ memories.py
   │     ├─ search.py
   │     ├─ state.py
   │     ├─ system.py
   │     └─ tools.py
   ├─ runtime\
   │  ├─ __init__.py
   │  ├─ runner.py
   │  ├─ task_runtime.py
   │  ├─ task_state.py
   │  ├─ task_events.py
   │  ├─ task_leases.py
   │  ├─ task_verifiers.py
   │  ├─ execution_segments.py
   │  ├─ queue_service.py
   │  ├─ executor.py
   │  ├─ verification.py
   │  ├─ repair.py
   │  ├─ recovery.py
   │  ├─ cancellation.py
   │  └─ multi_agent.py
   ├─ cognition\
   │  ├─ __init__.py
   │  ├─ planning.py
   │  ├─ semantic_planner.py
   │  ├─ reasoning_summary.py
   │  └─ output_protocol.py
   ├─ context\
   │  ├─ __init__.py
   │  ├─ service.py
   │  ├─ budget.py
   │  ├─ assembler.py
   │  └─ compiler.py
   ├─ personality\
   │  ├─ __init__.py
   │  ├─ identity_service.py
   │  ├─ identity_kernel.py
   │  ├─ identity_guard.py
   │  ├─ affect.py
   │  └─ agent_profiles.py
   ├─ memory\
   │  ├─ __init__.py
   │  ├─ service.py
   │  ├─ long_term.py
   │  └─ consolidation.py
   ├─ tools\
   │  ├─ __init__.py
   │  ├─ runtime_tools.py
   │  ├─ registry.py
   │  ├─ scheduler.py
   │  ├─ receipts.py
   │  ├─ mcp.py
   │  └─ skills.py
   ├─ workspace\
   │  ├─ __init__.py
   │  ├─ index.py
   │  ├─ instructions.py
   │  ├─ file_locks.py
   │  ├─ snapshots.py
   │  ├─ worktrees.py
   │  └─ lsp.py
   ├─ providers\
   │  ├─ __init__.py
   │  ├─ provider.py
   │  ├─ capabilities.py
   │  ├─ model_routing.py
   │  └─ web_search.py
   ├─ extensions\
   │  ├─ __init__.py
   │  ├─ sdk.py
   │  ├─ runtime.py
   │  └─ channels.py
   ├─ artifacts\
   │  ├─ __init__.py
   │  ├─ store.py
   │  └─ title_jobs.py
   ├─ security\
   │  ├─ __init__.py
   │  ├─ request_security.py
   │  ├─ network_security.py
   │  └─ trust.py
   ├─ kernel\
   │  ├─ __init__.py
   │  ├─ adapters.py
   │  ├─ contracts.py
   │  ├─ errors.py
   │  └─ services.py
   └─ evals\
      ├─ __init__.py
      ├─ cli.py
      ├─ comparison.py
      ├─ evidence.py
      ├─ full_function.py
      ├─ loader.py
      ├─ models.py
      ├─ reporting.py
      └─ runner.py
```

保留在 `app/` 根目录的模块均为跨领域入口或尚未纯化的共享边界。它们没有为了“目录好看”被强行归入错误领域。

## 3. 前端结构

```text
desktop\frontend\src\
├─ main.tsx
├─ App.tsx
├─ api.ts
├─ constants.ts
├─ types.ts
├─ desktopRuntime.ts
├─ secrets.ts
├─ buildInfo.ts
├─ buildInfoModel.ts
├─ characterAssets.ts
├─ adminActionGrants.ts
├─ hooks\
│  ├─ useAgentChat.ts
│  └─ useMediaQuery.ts
├─ components\
│  ├─ shared\
│  │  ├─ PanelHeader.tsx
│  │  ├─ CollapsibleSidebar.tsx
│  │  ├─ DesktopStatusBar.tsx
│  │  └─ TopHeader.tsx
│  ├─ chat\
│  │  ├─ ChatView.tsx
│  │  ├─ MainConversationArea.tsx
│  │  ├─ Composer.tsx
│  │  ├─ MessageItem.tsx
│  │  ├─ RecentConversationList.tsx
│  │  ├─ ConversationSummary.tsx
│  │  └─ AttachmentMenu.tsx
│  ├─ tasks\
│  │  ├─ TaskExecutionBlock.tsx
│  │  ├─ ReasoningTimeline.tsx
│  │  └─ ModelReasoningMenu.tsx
│  ├─ kokoro\
│  │  ├─ KokoroPanel.tsx
│  │  ├─ IdentityPanel.tsx
│  │  ├─ AffectStatePanel.tsx
│  │  ├─ CharacterCard.tsx
│  │  └─ CharacterPortrait.tsx
│  ├─ memory\
│  │  ├─ MemoryPanel.tsx
│  │  ├─ MemoryManager.tsx
│  │  └─ LongTermMemoryManager.tsx
│  ├─ providers\
│  │  ├─ DeepSeekProviderPanel.tsx
│  │  ├─ SearchProviderPanel.tsx
│  │  └─ UsagePanel.tsx
│  ├─ workspace\
│  │  ├─ FilesPanel.tsx
│  │  ├─ ProjectsPanel.tsx
│  │  └─ SearchPanel.tsx
│  └─ settings\
│     ├─ AboutPanel.tsx
│     ├─ AuditPanel.tsx
│     ├─ BackupPanel.tsx
│     ├─ ExtensionsPanel.tsx
│     ├─ PermissionMenu.tsx
│     └─ SetupDialog.tsx
├─ assets\
│  └─ characters\
│     ├─ kokoro-placeholder.svg
│     └─ natsume-kokoro.png   本地私有、Git 排除
└─ styles\
   ├─ tokens.css
   ├─ layout.css
   ├─ sidebar.css
   ├─ chat.css
   ├─ panels.css
   ├─ provider.css
   └─ responsive.css
```

## 4. 测试结构

```text
tests\backend\
├─ conftest.py
├─ api\
├─ cognition\
├─ context\
├─ evals\
├─ extensions\
├─ infrastructure\
├─ integration\
├─ kernel\
├─ memory\
├─ personality\
├─ providers\
├─ runtime\
├─ security\
├─ tools\
└─ workspace\
```

测试文件名与断言语义保持不变，只调整物理位置及三个依赖 `__file__` 的脚本路径。

## 5. 本地生成物与用户数据

本地生成物：

```text
build\
desktop\frontend\node_modules\
desktop\frontend\dist\
siyi\.venv\
%LOCALAPPDATA%\AureliusWu\AgentBuildCache\target\
司忆.exe
agent-backend.exe
```

真实用户数据仍位于仓库外：

```text
%LOCALAPPDATA%\AureliusWu\Agent\
%LOCALAPPDATA%\AureliusWu\Agent-Dev\
```

本次收敛不读取、修改或迁移真实用户数据。

## 6. 本地交付状态

- 版本仍为 `7.0.0`。
- 原头像与右侧夏目心摘要栏修复保留。
- 私有头像仍由 `.git/info/exclude` 排除。
- 结构收敛实施方案：`STRUCTURE_CONVERGENCE_EXECUTION_PLAN.md`。
- 完整执行审计：`STRUCTURE_CONVERGENCE_EXECUTION_REPORT.md`。
- 当前修改未提交、未推送。
