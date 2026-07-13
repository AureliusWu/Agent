# Agent

当前版本：`0.8.0`。

面向个人使用的通用 Agent：React/TypeScript 响应式 PWA、FastAPI + SQLite 后端，以及 Tauri 2 Windows 桌面壳。

## 当前能力

- OpenAI-compatible Provider，默认兼容 DeepSeek `deepseek-chat`，支持真实连接与延迟检查
- 持久化多轮对话、可配置的模型/工具/Token/时间预算、上下文压缩与无进展检测
- 用户选择的工作区沙箱，拒绝路径越界
- Codex 式三档权限：请求批准、替我审批、完全访问权限
- 工具注册表、严格参数 Schema、low/medium/high/critical 风险分级
- 文件上传、搜索、分段读取、编码/元数据、diff、创建、复制、修改、移动、删除
- 原子写入、逐任务备份、精确 patch/replace、diff、变更列表、单次或整任务撤销
- 受控命令执行：固定工作区、无 shell、超时限制，并遵守三档权限模式
- 兼容 `.agent/skills/*/SKILL.md` 与 `.codex/skills/*/SKILL.md`，相关 Skill 会进入模型上下文
- MCP 工具自动发现并进入模型工具目录；网页端支持远程 HTTP/SSE，桌面端可启用 stdio
- 显式“停止”按钮；同时中断浏览器请求和后端模型/MCP 协程，无需等待当前模型调用返回
- 一次性确认凭证绑定完整参数、任务和会话，支持允许一次/本任务/本会话并防重放
- 文件哈希、测试、构建和静态检查形成机器验证报告；未验证的代码任务只标记部分完成
- 执行轨迹展示任务、工具、Skill、授权、耗时、错误、验证分数与文件差异
- 工作区长期记忆经过权限系统保存，并始终作为不可信参考材料加载
- Skill 仅按任务相关性加载并记录使用情况，不会因数量少而默认全部注入
- 对话重命名/删除，MCP 与 Skill 启用/停用，MCP 连接测试接口
- Windows 单实例运行，重复启动时聚焦已有窗口
- 桌面 API Key 存入 Windows Credential Manager；网页端使用后端环境变量
- Windows 安装包内置 FastAPI sidecar，动态选择空闲端口并等待就绪，数据与轮转日志写入 `%LOCALAPPDATA%\AureliusWu\Agent`
- 固定 18 类真实任务的 Agent Eval：确定性运行时回归、DeepSeek 实盘评测、完整 Trace、JSON/Markdown 报告、历史、版本比较和稳定版门禁
- 独立 `Planner -> Executor -> Verifier -> Repair` 闭环：计划和验收条件先行，Executor 无权直接完成，Verifier 仅依据文件、命令、工具失败和副作用证据判定
- 验证失败后最多进行两次限定返工，只处理失败条件；计划、每次验证和返工记录可在审计页追溯

## 目录

- `frontend/src/components/`：聊天、侧栏、文件、扩展和审计界面
- `frontend/src/hooks/`：聊天任务与取消状态；`frontend/src/styles/`：组件级样式
- `frontend/src-tauri/`：Windows 桌面壳
- `backend/app/routes/`：按领域拆分的 FastAPI 路由
- `backend/app/task_runner.py`：Executor 循环、任务状态与即时取消
- `backend/app/planning.py`、`verification.py`、`repair.py`：计划、独立验证与限定返工
- `backend/app/evals/` 与 `backend/evals/`：评测运行器、证据规则、固定任务合同和发布策略
- `backend/app/`：SQLite、模型代理、沙箱、Skill/MCP 和工具注册表
- `scripts/`：Windows 开发与测试脚本

## 本地开发

```powershell
cd D:\AI项目\Agent
.\scripts\dev.ps1
```

首次运行会自动从 `backend/.env.example` 创建 `backend/.env`。网页模式在该文件配置 `AGENT_DEEPSEEK_API_KEY`；桌面模式可在扩展页保存到 Windows 凭据管理器。

浏览器打开 `http://localhost:5173`，API 文档位于 `http://127.0.0.1:8000/docs`。

## 测试与构建

```powershell
.\scripts\test.ps1
.\scripts\clean.ps1   # 清理可重新生成的构建产物
```

后端测试会生成覆盖率报告，并要求总体覆盖率不低于 `70%`；CI 使用同一门槛。

## Agent Eval

确定性评测驱动真实 Agent 循环、权限、工具、SQLite 和验证器，不调用付费模型：

```powershell
.\scripts\eval.ps1 -Mode scripted_runtime -Label local
```

真实模型评测只从当前进程读取 `AGENT_DEEPSEEK_API_KEY`，不会把密钥写入报告：

```powershell
$env:AGENT_DEEPSEEK_API_KEY = '<temporary-key>'
.\scripts\eval.ps1 -Mode live_model -Label deepseek
Remove-Item Env:AGENT_DEEPSEEK_API_KEY
```

报告和历史保存在忽略提交的 `data/evals/`。使用两个 `report.json` 运行 `python -m app.evals.cli compare` 可检测任务、成功率、虚假完成、耗时、Token、权限和恢复能力回退。稳定版必须通过：

```powershell
.\scripts\release-gate.ps1 -Report <report.json> -Baseline <previous-report.json>
```

当前确定性基线为 18/18，成功率 `100%`，虚假完成率为 0，稳定版门禁已通过。暂停、跨重启恢复、检查点选择、工作区漂移确认和副作用幂等均已进入固定回归测试。

## Windows 桌面端

Tauri 2 使用 Rust、Cargo 与 Microsoft C++ Build Tools。当前开发机已安装并通过 `cargo check`。运行：

```powershell
cd frontend
npm run tauri dev
npm run tauri build
```

桌面模式应让本机后端使用 `AGENT_ALLOW_LOCAL_MCP=true`。仓库已配置 Tauri 与 Windows Credential Manager 凭据命令。

## 安全边界

工作区必须是已存在目录。后端会规范化目标路径并验证解析后的真实路径仍位于工作区内。命令不经过 shell，当前目录必须位于工作区，输出和执行时间均有限制；交互式 shell、提权、格式化和系统控制命令被禁止。`完全访问权限` 只代表当前工作区文件权限，不代表整台电脑或系统权限。`.env`、数据库、密钥和构建产物均不得提交。

## 后续能力边界

`v0.8.0` 已完成第十轮长任务与可恢复执行，验收结果见 `ROUND10_RECOVERY_REPORT.md`。完整顺序见 `AGENT_NEXT_ROADMAP.md`，实施前审计见 `AGENT_ROADMAP_AUDIT.md`。下一步是第十一轮上下文与记忆工程；模型路由、多 Agent 和扩展 SDK 继续按顺序后置，插件市场与自动放宽权限仍未开放。
