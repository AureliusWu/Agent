# 司忆

当前版本：`2.0.0` Windows 桌面端 MVP。

面向个人使用的 Windows 通用 Agent：React/TypeScript 界面、FastAPI + SQLite 执行核心，以及 Tauri 2 桌面壳。PC 桌面端是当前唯一主产品；网页代码保留为开发基础，但本阶段冻结功能与适配。

## v2.0.0 验收状态

桌面 MVP 已通过完整测试（`238 passed, 1 skipped`，覆盖率 `83.41%`）、前端 lint/build/凭据扫描、Rust `4/4`、四组 Agent Eval、真实 DeepSeek 连接、首次启动与单实例验收，以及从 `v1.0.0` 覆盖升级的 NSIS/MSI 冒烟。升级会清理旧 `Agent.exe`，同时保留数据库、迁移备份和卸载后的用户数据。完整证据见 `docs/V2_DESKTOP_MVP_HANDOFF.md`。

## 当前能力

- DeepSeek 一等 OpenAI-compatible Provider：基础地址 `https://api.deepseek.com`，轻量/常规任务使用 `deepseek-v4-flash`，强推理使用 `deepseek-v4-pro`；失败和返工逐档升级
- Provider Adapter 统一完成、能力矩阵和探测合同；流式与原生工具调用按真实成功观测，视觉、音频和推理参数保留明确未知状态
- 模型路由结合近期样本成功率、能力和任务复杂度；样本不足时保持稳定，用户可手动选择自动/低/中/高推理强度或通过 API 指定精确模型
- 语义 Planner 先生成结构化任务合同，再由确定性校验和策略守卫收口；失败时自动回退安全预分类计划
- 持久化多轮对话、任务/阶段/单次调用三级 Token 预算、十字段结构化上下文压缩与无进展检测
- 用户选择的工作区沙箱，拒绝路径越界
- Codex 式三档权限：请求批准、替我审批、完全访问权限
- 工具注册表、严格参数 Schema、low/medium/high/critical 风险分级
- 文件上传、搜索、分段读取、编码/元数据、diff、创建、复制、修改、移动、删除
- 原子写入、逐任务备份、精确 patch/replace、diff、变更列表、单次或整任务撤销
- 受控命令执行：固定工作区、无 shell、超时限制，并遵守三档权限模式
- 兼容 `.agent/skills/*/SKILL.md` 与 `.codex/skills/*/SKILL.md`，只读取与当前任务匹配的少量 Skill
- 内置工具与 MCP 定义按计划和语义相关性限量注入；桌面端支持远程 HTTP/SSE，并可显式启用 stdio
- 显式“停止”按钮；同时中断浏览器请求和后端模型/MCP 协程，无需等待当前模型调用返回
- 一次性确认凭证绑定完整参数、任务和会话，支持允许一次/本任务/本会话并防重放
- Code、API、UI、Database、Security、Document 六类任务使用专用 Verifier；每条要求以 `requirement_id` 绑定真实证据，无关成功命令不能冒充验收，多模态验收接口已预留
- Runtime 通过稳定 `Executor` 合同执行工具；首个 `LocalWindowsExecutor` 负责能力声明、规范化工作区、工具执行、安全快照和暂停/取消/恢复/清理，旧 `ToolProvider` 仅保留为兼容门面
- 可重建工作区代码索引覆盖 Python、TypeScript、JavaScript 和 Rust，提供仓库地图、符号定义/引用、模块依赖、调用链、相关测试、Git 变更和解析诊断查询
- 执行轨迹展示任务、工具、Skill、授权、耗时、错误、验证分数、模型档位、阶段 Token、估算费用、缓存命中与文件差异
- 扩展页展示各模型能力状态、近期成功率、平均延迟和估算成本；能力未知不会被误报为支持
- 当前上下文、任务工作记忆、项目记忆和经验记忆分层管理，检查点与工作记忆在同一事务持久化
- 工作区记忆记录来源、适用版本、验证时间、置信度和使用成败；框架、依赖、目录变化或用户否定会自动降权或停用
- 工程记忆按架构、构建命令、测试命令、编码规范、技术决策、已知问题、成功修复、失败路径和用户约束分类；项目与个人命名空间隔离，个人记忆不进入 Agent 工程上下文
- 记忆写入遵守 `deny/explicit/allow` 策略：默认只响应用户明确的记住或忘记请求，只有 `allow` 才自动沉淀已验证修复或失败路径
- 扩展页可查看、创建、编辑、验证、否定和删除项目/经验记忆；所有记忆始终是不可信参考材料
- 对话重命名/删除，MCP 与 Skill 启用/停用，MCP 连接测试接口
- Windows 单实例运行，重复启动时聚焦已有窗口
- 桌面 API Key 存入 Windows Credential Manager；网页端使用后端环境变量
- Windows 安装包内置 FastAPI sidecar，动态选择空闲端口并异步等待就绪；异常退出有界重启，主程序退出或崩溃后 sidecar 会自动释放；数据与轮转日志写入 `%LOCALAPPDATA%\AureliusWu\Agent`
- 根目录 `VERSION` 是发布版本基准，Python/npm/Cargo 清单由 CI 一致性校验；三套依赖均使用提交的锁文件
- Windows 发布工作流生成 NSIS、MSI 与 CycloneDX SBOM，并实际执行旧版覆盖、桌面启动、schema 迁移、sidecar 清理、卸载和数据保留冒烟
- 数据库升级前自动创建一致性备份，失败时恢复原库；去敏诊断包只包含健康状态、最近审计摘要和截断日志
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
- 内置通用、编程、数据、文档和文件整理五类专业 Agent；每类拥有独立提示、工具白名单、Skill 标签、完成标准、Verifier 与默认权限
- 对话创建和输入区可持久选择专业 Agent，任务 Trace 固化配置快照；暂停期间扩展配置漂移会阻止任务继续
- 声明式扩展 SDK 支持工作区内扩展包安装、启停、依赖、完整性校验、版本并存和一键回滚
- 扩展工具只能代理现有受控工具，继续经过统一参数、权限、沙箱、审计和 Verifier；第三方任意代码不会被加载

## 目录

- `frontend/src/components/`：聊天、侧栏、文件、扩展和审计界面
- `frontend/src/hooks/`：聊天任务与取消状态；`frontend/src/styles/`：组件级样式
- `frontend/src-tauri/`：Windows 桌面壳
- `backend/app/routes/`：按领域拆分的 FastAPI 路由
- `backend/app/task_runner.py`：Executor 循环、任务状态、分层上下文与即时取消
- `backend/app/context.py`、`memory.py`：结构化压缩、工作记忆、按需检索、可信度与失效
- `backend/app/workspace_index.py`：有界源码扫描、语言索引、代码关系查询与源文件指纹缓存
- `backend/app/model_routing.py`、`efficiency.py`、`environment.py`：模型分档、成本估算、预算、压缩、并行与缓存
- `backend/app/planning.py`、`verification.py`、`repair.py`：计划、独立验证与限定返工
- `backend/app/evals/` 与 `backend/evals/`：评测运行器、证据规则、固定任务合同和发布策略
- `backend/app/trust.py`、`network_security.py`、`data_flow.py`、`snapshots.py`：不可信内容、出站网络、数据流和回滚边界
- `backend/app/diagnostics.py`：去敏诊断包；`backend/uv.lock` 与 `requirements.lock`：Python 可复现依赖
- `backend/app/multi_agent.py`、`file_locks.py`：子 Agent 调度契约、父子 Trace、文件范围和并发写锁
- `backend/app/agent_profiles.py`、`extension_sdk.py`、`extensions_runtime.py`：专业 Agent 配置与声明式扩展生命周期
- `backend/app/`：SQLite、模型代理、沙箱、Skill/MCP 和工具注册表
- `scripts/`：Windows 开发与测试脚本
- `司忆.exe`：根目录中的当前 Windows 桌面版；旁边的 `agent-backend.exe` 是必须保留的本地后端
- `docs/WINDOWS_RELEASE.md`：安装、升级、数据库恢复和发布验证
- `docs/PROJECT_HISTORY.md`：已合并的历史路线、版本与验收结论

## 桌面端开发

```powershell
cd <repository-root>
.\scripts\build-runtime.ps1 # 生成可直接运行的最新 司忆.exe
.\司忆.exe
```

桌面模式可在扩展页验证 DeepSeek API Key，验证成功后才写入 Windows 凭据管理器；失败不会覆盖原密钥，也可显式删除。首次启动必须主动选择工作区，应用不会默认访问 `<repository-parent>` 或整台电脑。

`scripts/dev.ps1` 仅保留给前后端联调；网页端当前冻结，不作为交付入口。

部署必须显式设置 `AGENT_DEPLOYMENT_MODE`：`desktop_local` 仅允许回环地址，`local_web` 用于本机网页开发，`web_control` 和 `cloud_executor` 属于非本地模式。任何非回环监听以及两个非本地模式都必须配置 `AGENT_API_TOKEN`，否则后端拒绝启动。监听地址由 `AGENT_BIND_HOST` 控制；不要把 FastAPI 端口直接暴露到公网。

模型路由可通过 `AGENT_MODEL_LIGHT_NAME`、`AGENT_MODEL_MEDIUM_NAME`、`AGENT_MODEL_STRONG_NAME` 配置；默认分别为 `deepseek-v4-flash`、`deepseek-v4-flash`、`deepseek-v4-pro`，界面直接显示真实模型名，不使用 Sonnet/Opus 等角色别名。如需显示美元估算，可用 `AGENT_MODEL_PRICING_JSON` 配置每百万输入/输出 Token 单价；未配置时界面只显示 Token 与耗时，不猜测价格。

远程网络默认仅允许公网 HTTPS。可通过 `AGENT_NETWORK_ALLOWED_DOMAINS` 与 `AGENT_NETWORK_BLOCKED_DOMAINS` 收紧域名范围；本地模型和私网 MCP 必须分别显式开启 `AGENT_ALLOW_PRIVATE_MODEL_PROVIDER` 与 `AGENT_ALLOW_LOCAL_MCP`。桌面 sidecar 会为每次进程启动生成独立 API 令牌。

多 Agent 默认可用但不会自动开启；聊天输入区显式选择模式。并发数、子 Agent 数量、Token 与超时上限通过 `AGENT_MULTI_AGENT_*` 环境变量收紧，子 Agent 永远不能派生下一层 Agent。

专业 Agent 在创建对话时选择，也可在空闲时切换。扩展页可从当前工作区安装声明式扩展包；示例路径为 `examples/extensions/team-coding`。格式、权限和回滚规则见 `EXTENSION_SDK.md`。

本地联调时 API 文档位于 `http://127.0.0.1:8000/docs`。

## 测试与构建

```powershell
.\scripts\test.ps1
cd frontend; npm run test:security; cd .. # 前端源码与构建产物凭据边界
.\scripts\smoke-sidecar.ps1 # 打包后端健康、版本、schema 与进程清理
.\scripts\build-desktop.ps1 # 锁定构建 NSIS/MSI、桌面生命周期冒烟与 SBOM
.\scripts\clean.ps1   # 清理可重新生成的构建产物
```

后端测试会生成覆盖率报告，并要求总体覆盖率不低于 `70%`；CI 使用同一门槛。

## 持久任务 API

- `POST /api/tasks`：创建后台任务并立即返回任务 ID。
- `GET /api/tasks/{task_id}`：查询任务状态与最终结果。
- `GET /api/tasks/{task_id}/events`：通过 SSE 获取可按 `Last-Event-ID` 续传的任务事件。
- `POST /api/tasks/{task_id}/pause|cancel|resume`：控制持久任务。
- `POST /api/chat`：兼容旧客户端的同步 Agent 主循环。

## Agent Eval

确定性评测驱动真实 Agent 循环、权限、工具、SQLite 和验证器，不调用付费模型：

```powershell
.\scripts\eval.ps1 -Mode scripted_runtime -Label local
.\scripts\eval.ps1 -Mode scripted_runtime -Label multi -Suite multi_agent -Tasks backend/evals/multi_agent_tasks.json
.\scripts\eval.ps1 -Mode scripted_runtime -Label professional -Suite professional_agents -Tasks backend/evals/professional_agent_tasks.json
.\scripts\eval.ps1 -Mode adversarial -Label security -Suite adversarial -Tasks backend/evals/adversarial_tasks.json
```

日常开发默认运行 5 个代表性核心任务与相关单元测试；完整 18 项核心、多 Agent、专业 Agent 和真实模型评测仅用于路线图里程碑、运行时或权限边界变更、数据库迁移和发布候选。CI 会校验全部合同并运行紧凑的对抗门禁；真实模型抽检通过手动 GitHub Actions 工作流触发。

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

当前核心确定性基线为 18/18，多 Agent 基线为 3/3，专业 Agent 基线为 4/4。三者成功率均为 `100%`，虚假完成、权限违规、沙箱违规和无关文件修改均为 0。平均任务耗时回归门槛已收紧为相对基线不超过 `15%`。

## Windows 桌面端

Tauri 2 使用 Rust、Cargo 与 Microsoft C++ Build Tools。当前开发机已安装并通过 `cargo check`。运行：

```powershell
.\司忆.exe                  # 日常直接启动当前桌面版
.\scripts\build-runtime.ps1 # 源码修改后重新生成根目录 司忆.exe
.\scripts\build-desktop.ps1
```

桌面模式应让本机后端使用 `AGENT_ALLOW_LOCAL_MCP=true`。仓库已配置 Tauri 与 Windows Credential Manager 凭据命令。Rust 编译缓存位于 `%LOCALAPPDATA%\AureliusWu\AgentBuildCache`，不再堆积在项目目录；`司忆.exe` 与 `agent-backend.exe` 位于项目根目录并忽略提交。构建脚本会清理应用增量缓存，确保桌面程序嵌入当前前端，而不是历史页面。

## 安全边界

工作区必须是已存在目录。后端会规范化目标路径并验证解析后的真实路径仍位于工作区内。命令不经过 shell，当前目录必须位于工作区，输出和执行时间均有限制；交互式 shell、提权、格式化和系统控制命令被禁止。`完全访问权限` 只代表当前工作区文件权限，不代表整台电脑或系统权限。安全快照保存在本机应用数据目录并按数量轮换，不上传云端；快照可能包含项目敏感文件，应沿用当前 Windows 用户权限保护。完整威胁模型见 `SECURITY_MODEL.md`。`.env`、数据库、密钥和构建产物均不得提交。

## 后续能力边界

`v2.0.0` 将 PC 桌面端确立为唯一主产品，收口 sidecar 生命周期、启动状态、首选工作区、API Key 验证、任务暂停/停止/恢复、角色资源和安装升级链路。`v1.0.0` 是本版本的覆盖安装基线；历史路线与发布证据见 `docs/PROJECT_HISTORY.md`。网页端、移动端、云同步、多人协作、最终视觉重构和角色动画均暂缓。插件市场、第三方任意代码、任意深度子 Agent 与自动放宽权限仍未开放。
