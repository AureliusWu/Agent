# 司忆

当前定向发布验收由 `evals/full_function_manifest_v2.json` 驱动，共 180 项（P0 125、P1 54、P2 1）。原始人工说明与产品决策保存在 `docs/acceptance/v4-targeted/`；机器报告必须区分自动化门禁、真实桌面场景和未执行项，不能用普通单元测试冒充手动或 E2E 证据。

当前版本：`16.0.0` Windows PC 桌面端 Agent Runtime。本版强化桌面文件工作流：冲突安全撤销、持久化批次步骤与中断核对、批量改名/分类移动/字面替换/最近批次恢复，以及有界文件扫描、分页恢复区和备份容量检查。Provider 使用当前端点、模型摘要与配置对应的有效上下文预算；设置页区分基础对话、只读工具、结构化计划和文件 Agent 四级资格。MCP 分页、跨会话调度、语音诊断与 80% 测试门禁同时收敛。当前为开发候选；完成程度和实际验收缺口见 `docs/16.0.0/RELEASE_STATUS.json`，安装、真实模型、麦克风与正式分发以本版本绑定证据确认。

本地语音输入在可用内存低于默认 2 GiB 安全下限时会暂停本次操作。录音区与设置页会显示中文原因；后端提供数值时，还会显示本次检查的可用内存和下限。保存工作并关闭占用内存较多的程序后，可直接重新录音，无需重启司忆，文字输入仍可使用。失败的录音不会自动重放；临时音频和麦克风资源会释放。

Ollama 离线时，本地 AI 资源面板仍可读取系统内存和语音准入状态；Ollama 的模型状态标为未观察到。

## v6.0.0 持续执行与自动工具调度

v6.0.0 将轮数、工具次数、上下文压力和单段超时从“任务终止条件”改为执行分段边界：运行时保存结构化检查点、压缩上下文并继续，只有管理员停止、待授权、安全阻断、连续无进展或不可恢复故障才结束。对外统一为“基础Agent”，只读工具可有界并行，写入串行，高风险调用独占；DeepSeek 原生推理与 DSML 降级工具协议均可安全回放。项目指令按目录层级加载 `AGENTS.md`/`AGENTS.override.md`，README 默认只是资料。联网搜索支持 Tavily 与 Brave 可选供应商、独立安全 `web_fetch` 和可核验来源，未配置的供应商不会暴露给模型。实施与验收见 `docs/V6_0_0_PLAN.md`。

## v5.0.0 能力真实性与验收

v5.0.0 建立运行时能力真相源：无工作区聊天、工作区工具和远程 MCP 分别说明可用条件。MCP 新服务必须通过真实工具发现才能启用，扩展页展示最近健康状态、可用工具数量和去敏错误。180 项定向验收要求命令、制品、构建 ID 与时间齐全；手动或 E2E 用例没有独立场景证据时保持阻塞，禁止用汇总门禁冒充单项通过。实施顺序与边界见 `docs/V5_0_0_PLAN.md`。

## v4.0.3 最终候选

v4.0.3 固化 4.0 定向验收结果，验收报告版本改为从正式 Sidecar 构建信息自动读取，避免测试文档与实际制品版本漂移。发布候选继续要求全量测试、真实身份与工作区链路、跨版本安装升级、核心/安全/多 Agent/专业 Agent 评测全部通过。

## v4.0.2 定向回归

v4.0.2 修复测试链路对旧构建指纹的顺序依赖，并在 Provider 边界拦截未实际执行的伪工具协议文本。LSP 的协议故障降级和工作区外位置过滤也纳入自动回归；正式安装包使用 v4.0.1 安装器执行覆盖升级验证。

## v4.0.1 定向验收基线

v4.0.1 接入 180 项定向测试 manifest，补齐公开推理摘要、真实运行日期、活动对话删除与 Profile 切换保护、LSP 故障降级、Hook 隔离脱敏，以及受管 Worktree 跨进程写锁。该版本用于首次完整测试并收集 4.0.2 的失败修复清单。

面向个人使用的 Windows 通用 Agent：React/TypeScript 界面、FastAPI + SQLite 执行核心，以及 Tauri 2 桌面壳。v8.0.3 起主仓库只维护 PC 桌面端；网页/PWA 与非 PC 部署实现已剥离到相邻的 `../Agent非PC端`。

## v4.0.0 核心升级

v4.0.0 完成参考能力中尚缺的桌面运行时能力：真实 LSP JSON-RPC 源码导航支持 Python、TypeScript/JavaScript 和 Rust，未安装语言服务器时明确降级到工作区索引；Hook 生命周期覆盖工具、上下文压缩与任务完成，单个扩展失败不影响核心任务；MCP 增加 TTL 会话复用、配置指纹、过期和断线重建；Git Worktree 只能在 `.agent/worktrees` 受管目录中创建，并纳入统一权限、锁和审计链。完整排序与验收边界见 `docs/V4_0_0_REQUIREMENTS_MATRIX.md`。

## v3.0.0 核心升级

v3.0.0 将任务执行升级为可持续干预的运行时：SQLite 持久队列是唯一事实源，同一对话保持单执行链；运行中输入可选择排队或在安全点引导当前任务。停止、引导、提升优先级和取消排队项使用独立语义。工具层新增中断/并发策略、结构化回执和大输出制品，保留既有文件读取缓存、无进展检测及 Windows 进程树终止。实现与验证矩阵见 `docs/V3_0_0_REQUIREMENTS_MATRIX.md`。

## v2.0.1 验收状态

v2.0.1 在桌面 MVP 上增加固定身份、统一长期记忆、情绪与关系状态、动态模型上下文、可追溯连续性、完整备份恢复和自动构建指纹。发布候选已通过后端 `273 passed, 1 skipped`（覆盖率 `82.31%`）、前端 lint/build/构建一致性测试、Rust `4/4`、核心 Eval `18/18`，以及真实 DeepSeek 身份/工作区验收。自动验收记录见 `docs/acceptance/`；最终状态仍需管理员打开正式程序确认。

## v2.0.0 验收状态

桌面 MVP 已通过完整测试（`238 passed, 1 skipped`，覆盖率 `83.41%`）、前端 lint/build/凭据扫描、Rust `4/4`、四组 Agent Eval、真实 DeepSeek 连接、首次启动与单实例验收，以及从 `v1.0.0` 覆盖升级的 NSIS/MSI 冒烟。升级会清理旧 `Agent.exe`，同时保留数据库、迁移备份和卸载后的用户数据。完整证据见 `docs/V2_DESKTOP_MVP_HANDOFF.md`。

## 当前能力

- 固定身份内核：`agent_id=natsume-kokoro-001`，身份版本只能由管理员明确确认后切换；身份在 Provider、模型、新对话和重启之间保持连续，Identity Guard 会修复明确的基座身份漂移
- 动态上下文能力表：按 Provider/模型确定上下文窗口、输出预留、Provider 开销和安全余量；未知模型使用保守回退，接近窗口时确定性压缩历史并保留身份、当前用户消息和工具协议
- 统一长期记忆区分语义、情景、程序与关系记忆，记录来源、可信度、重要性、有效时间、确认、锁定和敏感状态；模型只能提交候选，管理员决定是否写入
- 新事实可替代旧事实并保留历史；锁定冲突进入人工处理，软删除墓碑阻止模型自动恢复已遗忘内容；检索综合关键词、时效、重要性、可信度和确认来源
- Trait、Mood、Emotion 与关系状态保存在 SQLite；即时情绪随时间衰减，普通与重大关系事件分别限幅，而且状态只允许影响语气与关注方式
- Context Assembler 统一装载身份、情绪关系、相关长期记忆、专业 Profile 与当前任务，并记录引用的记忆 ID；安全调试接口不返回敏感正文
- 记忆整理器可归档低价值旧推断，并根据身份、关系、来源记忆与未完成任务确定性生成连续性摘要和 `kokoro_autobiography.md`，不进行文学补全
- 完整备份包包含身份、对话、任务、长期记忆、情绪和关系状态；导出明确提示敏感性，恢复前自动备份当前库，并校验格式、身份、Schema、SHA-256 和 SQLite 完整性

- DeepSeek 一等 OpenAI-compatible Provider：基础地址 `https://api.deepseek.com`，轻量/常规任务使用 `deepseek-v4-flash`，强推理使用 `deepseek-v4-pro`；失败和返工逐档升级
- Provider Adapter 统一完成、能力矩阵和探测合同；流式与原生工具调用按真实成功观测，视觉、音频和推理参数保留明确未知状态
- 模型路由结合近期样本成功率、能力和任务复杂度；样本不足时保持稳定，用户可手动选择自动/低/中/高推理强度或通过 API 指定精确模型
- 语义 Planner 先生成结构化任务合同，再由确定性校验和策略守卫收口；失败时自动回退安全预分类计划
- 持久化多轮对话与 ExecutionSegment；默认 Token、轮数、工具次数和单段超时只触发检查点、压缩与续跑，管理员可显式设置费用上限，无进展和不可恢复故障仍会安全停止
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
- API Key 存入 Windows Credential Manager
- Windows 安装包内置 FastAPI sidecar，动态选择空闲端口并异步等待就绪；异常退出有界重启，主程序退出或崩溃后 sidecar 会自动释放；生产运行数据写入 `%LOCALAPPDATA%\AureliusWu\Agent`，开发数据隔离到同级 `Agent-Dev`，数据库固定使用各自的 `data/agent.db`
- 构建阶段自动采集 Git 提交、分支、CLEAN/DIRTY、源码内容指纹、时间、类型和 Schema；Tauri、React 与 Python Sidecar 共享同一构建 ID，设置页可复制完整信息，侧栏展示简略指纹，不一致或组件缺失会明确告警
- 根目录 `VERSION` 是发布版本基准，Python/npm/Cargo 清单由 CI 一致性校验；三套依赖均使用提交的锁文件
- Windows 发布工作流生成 NSIS、MSI、CycloneDX SBOM 与第三方许可证声明，并实际执行旧版覆盖、桌面启动、schema 迁移、sidecar 清理、卸载和数据保留冒烟
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
- 对外只提供“基础Agent”并自动调度工具；历史多 Agent 字段保持数据库兼容，但新任务固定单 Agent，不再暴露 Profile 或编排模式选择
- 统一 ToolScheduler 依据 `parallel_safe/serial/exclusive` 调度，只读调用有界并行，写入串行，高风险调用独占，失败隔离且结果按调用 ID 稳定回传
- 跨任务文件锁记录修改前后版本；同文件并发写或命令级工作区冲突会暂停，绝不自动覆盖或静默合并
- 审计页展示父子 Agent、角色、状态、预算、实际 Token 与文件锁；完整 Trace 可通过 `/api/tasks/{task_id}/agents` 获取
- 旧专业 Agent 标识在读取历史任务时映射到基础Agent；扩展工具、Skills、Hooks、MCP、权限与 Verifier 继续保留，不再形成可选择的人格/Profile
- 声明式扩展 SDK 支持工作区内扩展包安装、启停、依赖、完整性校验、版本并存和一键回滚
- 扩展工具只能代理现有受控工具，继续经过统一参数、权限、沙箱、审计和 Verifier；第三方任意代码不会被加载

## 目录

- `desktop/frontend/src/components/`：聊天、侧栏、文件、扩展和审计界面
- `desktop/frontend/src/hooks/`：聊天任务与取消状态；`desktop/frontend/src/styles/`：组件级样式
- `desktop/src-tauri/`：Windows 桌面壳
- `siyi/app/routes/`：按领域拆分的 FastAPI 路由
- `siyi/app/runtime/runner.py`：Executor 循环、任务状态、分层上下文与即时取消
- `siyi/app/context/service.py`、`memory/service.py`：结构化压缩、工作记忆、按需检索、可信度与失效
- `siyi/app/workspace/index.py`：有界源码扫描、语言索引、代码关系查询与源文件指纹缓存
- `siyi/app/model_routing.py`、`efficiency.py`、`environment.py`：模型分档、成本估算、预算、压缩、并行与缓存
- `siyi/app/planning.py`、`verification.py`、`repair.py`：计划、独立验证与限定返工
- `siyi/app/evals/` 与 `evals/`：评测运行器、证据规则、固定任务合同和发布策略
- `siyi/app/trust.py`、`network_security.py`、`data_flow.py`、`snapshots.py`：不可信内容、出站网络、数据流和回滚边界
- `siyi/app/diagnostics.py`：去敏诊断包；`siyi/uv.lock` 与 `requirements.lock`：Python 可复现依赖
- `siyi/app/build_info.py` 与 `scripts/generate_build_info.py`：读取制品内嵌清单及生成统一构建指纹；正式安装后不依赖 Git
- `siyi/app/multi_agent.py`、`file_locks.py`：子 Agent 调度契约、父子 Trace、文件范围和并发写锁
- `siyi/app/agent_profiles.py`、`extension_sdk.py`、`extensions_runtime.py`：专业 Agent 配置与声明式扩展生命周期
- `siyi/app/`：SQLite、模型代理、沙箱、Skill/MCP 和工具注册表
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

桌面模式可在扩展页验证 DeepSeek API Key，验证成功后才写入 Windows 凭据管理器；失败不会覆盖原密钥，也可显式删除。普通聊天不需要工作区，也不会获得本地文件工具。只有进入“项目”并主动选择目录后，Agent 才能读取或修改该项目；应用不会默认访问仓库上级目录或整台电脑。

`scripts/dev.ps1` 仅用于 PC 桌面界面与本机 sidecar 联调，不是网页产品入口。

`AGENT_DEPLOYMENT_MODE` 固定为 `desktop_local`，只允许回环地址。监听地址由 `AGENT_BIND_HOST` 控制；FastAPI 端口不得暴露到局域网或公网。

模型路由可通过 `AGENT_MODEL_LIGHT_NAME`、`AGENT_MODEL_MEDIUM_NAME`、`AGENT_MODEL_STRONG_NAME` 配置；默认分别为 `deepseek-v4-flash`、`deepseek-v4-flash`、`deepseek-v4-pro`，界面直接显示真实模型名，不使用 Sonnet/Opus 等角色别名。如需显示美元估算，可用 `AGENT_MODEL_PRICING_JSON` 显式配置完整、有限、非负的每百万输入/输出 Token 单价。未配置单价、缺失用量或历史费用不明确时显示“费用未知”，不能把未知当作零费用；已知部分单独列出。定价绑定端点和模型，不向任意兼容网关继承。详见 `docs/16.0.0/COST_SAFETY.md`。

远程网络默认仅允许公网 HTTPS。可通过 `AGENT_NETWORK_ALLOWED_DOMAINS` 与 `AGENT_NETWORK_BLOCKED_DOMAINS` 收紧域名范围；本地模型和私网 MCP 必须分别显式开启 `AGENT_ALLOW_PRIVATE_MODEL_PROVIDER` 与 `AGENT_ALLOW_LOCAL_MCP`。桌面 sidecar 会为每次进程启动生成独立 API 令牌。

Agent 对外统一为基础Agent，模型根据 ToolSpec 自动选择工具，ToolScheduler 决定并行、串行或独占执行。默认不设任务 Token 硬终止：`AGENT_MAX_AGENT_ROUNDS`、`AGENT_MAX_TOOL_CALLS` 与 `AGENT_TASK_TIMEOUT_SECONDS` 是单个 ExecutionSegment 的边界；达到边界后保存检查点并续跑。请求中显式提供美元预算时启用调用前费用预留与后续调用停止门禁；费用未知则安全停止，不自动补价格、重试或改用更贵模型。输入 Token 是估值，因此这是估计式支出控制，不是服务商实际账单的硬上界；独立 Token、时间和上下文限制仍保留。

扩展页可从当前工作区安装声明式扩展包；示例路径为 `examples/extensions/team-coding`。格式、权限和回滚规则见 `EXTENSION_SDK.md`。

本地联调时 API 文档位于 `http://127.0.0.1:8000/docs`。

## 测试与构建

```powershell
.\scripts\test.ps1
cd desktop/frontend; npm run test:security; cd ../.. # 前端源码与构建产物凭据边界
.\scripts\smoke-sidecar.ps1 # 打包后端健康、版本、schema 与进程清理
.\scripts\build-desktop.ps1 # 锁定构建 NSIS/MSI、桌面生命周期冒烟与 SBOM
.\siyi\.venv\Scripts\python.exe .\scripts\acceptance_identity_workspace.py # 需临时 SIYI_ACCEPTANCE_MODEL_KEY，运行真实身份/工作区验收
.\siyi\.venv\Scripts\python.exe .\scripts\acceptance-v6-continuity.py # 需临时 AGENT_DEEPSEEK_API_KEY，真实 30 轮无工作区连续性
.\siyi\.venv\Scripts\python.exe .\scripts\acceptance-v6-search.py # 需临时 AGENT_TAVILY_API_KEY 与模型密钥，真实搜索和来源验收
.\scripts\clean.ps1   # 清理可重新生成的构建产物
```

后端测试会生成覆盖率报告，并要求总体覆盖率不低于 `70%`；CI 使用同一门槛。

首次克隆后运行 `.\scripts\install-dev-hooks.ps1`，启用仓库内的
pre-commit 隐私扫描；合成或真实的密钥、私人路径和运行数据都会在提交前被阻断。

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
.\scripts\eval.ps1 -Mode scripted_runtime -Label multi -Suite multi_agent -Tasks evals/multi_agent_tasks.json
.\scripts\eval.ps1 -Mode scripted_runtime -Label professional -Suite professional_agents -Tasks evals/professional_agent_tasks.json
.\scripts\eval.ps1 -Mode adversarial -Label security -Suite adversarial -Tasks evals/adversarial_tasks.json
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

`v2.0.0` 将 PC 桌面端确立为唯一主产品，收口 sidecar 生命周期、启动状态、首选工作区、API Key 验证、任务暂停/停止/恢复、角色资源和安装升级链路。`v1.0.0` 是本版本的覆盖安装基线；历史路线与发布证据见 `docs/PROJECT_HISTORY.md`。v8.0.3 将网页/PWA、移动端与非 PC 部署代码移出主仓库；云同步、多人协作、最终视觉重构和角色动画仍未实施。插件市场、第三方任意代码、任意深度子 Agent 与自动放宽权限仍未开放。
