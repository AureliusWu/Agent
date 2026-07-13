# Agent

当前版本：`0.12.0`。

面向个人使用的通用 Agent：React/TypeScript 响应式 PWA、FastAPI + SQLite 后端，以及 Tauri 2 Windows 桌面壳。

## 当前能力

- OpenAI-compatible Provider，默认兼容 DeepSeek `deepseek-chat`；轻量/中等/强模型按任务类型路由，失败和返工逐档升级
- 持久化多轮对话、任务/阶段/单次调用三级 Token 预算、十字段结构化上下文压缩与无进展检测
- 用户选择的工作区沙箱，拒绝路径越界
- Codex 式三档权限：请求批准、替我审批、完全访问权限
- 工具注册表、严格参数 Schema、low/medium/high/critical 风险分级
- 文件上传、搜索、分段读取、编码/元数据、diff、创建、复制、修改、移动、删除
- 原子写入、逐任务备份、精确 patch/replace、diff、变更列表、单次或整任务撤销
- 受控命令执行：固定工作区、无 shell、超时限制，并遵守三档权限模式
- 兼容 `.agent/skills/*/SKILL.md` 与 `.codex/skills/*/SKILL.md`，只读取与当前任务匹配的少量 Skill
- 内置工具与 MCP 定义按计划和语义相关性限量注入；网页端支持远程 HTTP/SSE，桌面端可启用 stdio
- 显式“停止”按钮；同时中断浏览器请求和后端模型/MCP 协程，无需等待当前模型调用返回
- 一次性确认凭证绑定完整参数、任务和会话，支持允许一次/本任务/本会话并防重放
- 文件哈希、测试、构建和静态检查形成机器验证报告；未验证的代码任务只标记部分完成
- 执行轨迹展示任务、工具、Skill、授权、耗时、错误、验证分数、模型档位、阶段 Token、估算费用、缓存命中与文件差异
- 当前上下文、任务工作记忆、项目记忆和经验记忆分层管理，检查点与工作记忆在同一事务持久化
- 工作区记忆记录来源、适用版本、验证时间、置信度和使用成败；框架、依赖、目录变化或用户否定会自动降权或停用
- 扩展页可查看、创建、编辑、验证、否定和删除项目/经验记忆；所有记忆始终是不可信参考材料
- 对话重命名/删除，MCP 与 Skill 启用/停用，MCP 连接测试接口
- Windows 单实例运行，重复启动时聚焦已有窗口
- 桌面 API Key 存入 Windows Credential Manager；网页端使用后端环境变量
- Windows 安装包内置 FastAPI sidecar，动态选择空闲端口并等待就绪，数据与轮转日志写入 `%LOCALAPPDATA%\AureliusWu\Agent`
- 固定 18 类真实任务的 Agent Eval：确定性运行时回归、DeepSeek 实盘评测、完整 Trace、JSON/Markdown 报告、历史、版本比较和稳定版门禁
- 独立 `Planner -> Executor -> Verifier -> Repair` 闭环：计划和验收条件先行，Executor 无权直接完成，Verifier 仅依据文件、命令、工具失败和副作用证据判定
- 验证失败后最多进行两次限定返工，只处理失败条件；计划、每次验证和返工记录可在审计页追溯
- 工具结果按类型去重和压缩，所有截断均明确标记；完整原始结果只保留在审计记录，不重复注入模型
- 只读工具可安全并行并在任务内缓存；Skill 内容、项目指纹和构建环境检测按文件状态失效，任何写入都会清空相关缓存
- 本地 API 进程令牌、对话/任务/工作区三重绑定与细粒度能力令牌，批准内容无法跨工作区或任务复用
- README、项目文件、Skill、记忆与 MCP 返回值统一标记为不可信数据；检测到提示词注入后，副作用操作自动降级为请求批准
- 模型请求先去除凭据；MCP 密钥型参数默认阻止外发，日志只保存去敏内容，审计页展示数据流、阻止次数和去敏计数
- 模型与远程 MCP 共用网络策略：拒绝内网、回环、云元数据、明文 HTTP、跨域凭据跳转、附件和超大响应；支持域名黑白名单
- 命令与 MCP 调用前保存工作区、Git、任务状态和 SQLite 安全快照；支持差异预览、恢复前二次快照和受控回滚
- 可选单 Agent、规划执行、生成验证、并行探索四种模式；子 Agent 具有独立任务、输出、Token、工具、文件、时间和风险边界
- Planner 与 Explorer 只能使用范围内只读工具；Generator 的独立 Verifier 可要求一次限定返工，根 Agent 始终是唯一写入者
- 跨任务文件锁记录修改前后版本；同文件并发写或命令级工作区冲突会暂停，绝不自动覆盖或静默合并
- 审计页展示父子 Agent、角色、状态、预算、实际 Token 与文件锁；完整 Trace 可通过 `/api/tasks/{task_id}/agents` 获取

## 目录

- `frontend/src/components/`：聊天、侧栏、文件、扩展和审计界面
- `frontend/src/hooks/`：聊天任务与取消状态；`frontend/src/styles/`：组件级样式
- `frontend/src-tauri/`：Windows 桌面壳
- `backend/app/routes/`：按领域拆分的 FastAPI 路由
- `backend/app/task_runner.py`：Executor 循环、任务状态、分层上下文与即时取消
- `backend/app/context.py`、`memory.py`：结构化压缩、工作记忆、按需检索、可信度与失效
- `backend/app/model_routing.py`、`efficiency.py`、`environment.py`：模型分档、成本估算、预算、压缩、并行与缓存
- `backend/app/planning.py`、`verification.py`、`repair.py`：计划、独立验证与限定返工
- `backend/app/evals/` 与 `backend/evals/`：评测运行器、证据规则、固定任务合同和发布策略
- `backend/app/trust.py`、`network_security.py`、`data_flow.py`、`snapshots.py`：不可信内容、出站网络、数据流和回滚边界
- `backend/app/multi_agent.py`、`file_locks.py`：子 Agent 调度契约、父子 Trace、文件范围和并发写锁
- `backend/app/`：SQLite、模型代理、沙箱、Skill/MCP 和工具注册表
- `scripts/`：Windows 开发与测试脚本

## 本地开发

```powershell
cd D:\AI项目\Agent
.\scripts\dev.ps1
```

首次运行会自动从 `backend/.env.example` 创建 `backend/.env`。网页模式在该文件配置 `AGENT_DEEPSEEK_API_KEY`；桌面模式可在扩展页保存到 Windows 凭据管理器。

模型路由可通过 `AGENT_MODEL_LIGHT_NAME`、`AGENT_MODEL_MEDIUM_NAME`、`AGENT_MODEL_STRONG_NAME` 配置；留空时三档都回退到 `AGENT_MODEL_NAME`。如需显示美元估算，可用 `AGENT_MODEL_PRICING_JSON` 配置每百万输入/输出 Token 单价；未配置时界面只显示 Token 与耗时，不猜测价格。

远程网络默认仅允许公网 HTTPS。可通过 `AGENT_NETWORK_ALLOWED_DOMAINS` 与 `AGENT_NETWORK_BLOCKED_DOMAINS` 收紧域名范围；本地模型和私网 MCP 必须分别显式开启 `AGENT_ALLOW_PRIVATE_MODEL_PROVIDER` 与 `AGENT_ALLOW_LOCAL_MCP`。桌面 sidecar 会为每次进程启动生成独立 API 令牌。

多 Agent 默认可用但不会自动开启；聊天输入区显式选择模式。并发数、子 Agent 数量、Token 与超时上限通过 `AGENT_MULTI_AGENT_*` 环境变量收紧，子 Agent 永远不能派生下一层 Agent。

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
.\scripts\eval.ps1 -Mode scripted_runtime -Label multi -Suite multi_agent -Tasks backend/evals/multi_agent_tasks.json
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

当前核心确定性基线为 18/18；独立多 Agent 评测集连续三次 3/3。两者成功率均为 `100%`，虚假完成、权限违规、沙箱违规和无关文件修改均为 0。平均任务耗时回归门槛已收紧为相对基线不超过 `15%`。

## Windows 桌面端

Tauri 2 使用 Rust、Cargo 与 Microsoft C++ Build Tools。当前开发机已安装并通过 `cargo check`。运行：

```powershell
cd frontend
npm run tauri dev
npm run tauri build
```

桌面模式应让本机后端使用 `AGENT_ALLOW_LOCAL_MCP=true`。仓库已配置 Tauri 与 Windows Credential Manager 凭据命令。

## 安全边界

工作区必须是已存在目录。后端会规范化目标路径并验证解析后的真实路径仍位于工作区内。命令不经过 shell，当前目录必须位于工作区，输出和执行时间均有限制；交互式 shell、提权、格式化和系统控制命令被禁止。`完全访问权限` 只代表当前工作区文件权限，不代表整台电脑或系统权限。安全快照保存在本机应用数据目录并按数量轮换，不上传云端；快照可能包含项目敏感文件，应沿用当前 Windows 用户权限保护。完整威胁模型见 `SECURITY_MODEL.md`。`.env`、数据库、密钥和构建产物均不得提交。

## 后续能力边界

`v0.12.0` 已完成第十四轮受控多 Agent，验收结果见 `ROUND14_MULTI_AGENT_REPORT.md`；第十三轮高级安全见 `ROUND13_SECURITY_REPORT.md`。完整顺序见 `AGENT_NEXT_ROADMAP.md`。下一步严格进入第十五轮专业 Agent 与扩展 SDK；插件市场、任意深度子 Agent 与自动放宽权限仍未开放。
