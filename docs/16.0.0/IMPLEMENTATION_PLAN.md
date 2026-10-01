# 司忆 v16.0.0 更新方案

编制日期：2026-09-30。项目：司忆 Agent（本仓库）。状态：规划稿，尚未实施。

本方案供项目负责人和后续开发 AI 使用。建议 v16.0.0 以**可靠的桌面文件任务执行**为主线：让用户看清将修改什么，在文件变化、程序中断或资源不足时安全停止，并能依据真实记录继续或恢复。保留司忆的个人 Agent、人格和长期记忆定位，不改成另一类产品。

架构选择为 **B 局部重构**。继续使用 React、Tauri、FastAPI、SQLite、现有 Executor、权限内核和可撤销备份，不另建第二套执行系统。先补数据安全与恢复合同，再完善用户工作流；截图、浏览器控制、跨设备和无人值守扩张不进入本版必选范围。

本文件是方案，不是实施或发布授权。此次只新增本文，没有更改产品源码、依赖、版本号、数据库、安装程序或发布状态。

## 一 当前基线及证据适用范围

| 项目 | 2026-09-30 核对结果 | 规划含义 |
| --- | --- | --- |
| 源码版本 | `VERSION` 为 `15.0.0`，15 处版本来源检查通过 | 不因制定 v16 方案立即改版本号 |
| 源码状态 | `local/v15.0.0`，HEAD `7449c43068a8fb0bf5154666547184d2fbf75fa2`，有未提交语音修复及原有交接文件 | HEAD 不等于全部已交付内容；保留并逐项归属这些变更 |
| 根目录便携程序 | 随包 manifest 为 v15.0.0、schema 45、`DIRTY`；build_id 为 `8259529285d96854bf0bfc76` | 9 月 16 日修复已经交付便携版；本轮只核对记录，未重新启动程序 |
| 正式门禁文档 | `docs/15.0.0` 仍记 28/31，通过记录主要绑定旧提交 `2f67faa` | 是历史证据，不是当前工作树完整验收，也不能直接继承为 v16 PASS |
| DeepSeek | 首次失败后，9 月 6 日第二次最小 HTTP 付费验证已有 PASS，13 tokens，绑定旧源码 | 不能继续笼统称 DeepSeek 验证失败；也不能把直连响应当当前桌面 Agent 全链路通过 |
| 语音修复 | 9 月 16 日报告记录 85 项定向后端、11 项 Rust、前端回归、3 项 scripted Eval、冻结程序及便携生命周期通过 | 本轮没有重跑这些套件；真实麦克风到 small 模型转写仍未完成验收 |
| 本地模型 | 历史 `qwen2.5:1.5b` Basic 为 3/3，完整基准为 10/19 | 只代表指定模型和指定测试的结果，不能称本地文件 Agent 已全面可靠 |
| 正式发行 | 本地未找到正式 v15 tag；随包 release 状态为 `PARTIAL / BLOCKED / NOT_READY` | 远端发行状态未查询，不从本地状态推断远端一定未发行 |

证据来源：`docs/15.0.0/RELEASE_STATUS.json`、`TEST_MATRIX.json`、`MODEL_BENCHMARK_REPORT.md`；`build/v1500-evidence/deepseek-paid-live-second-http.json`；`build/voice-ram-fix-20260915/VERIFICATION.md`；`_internal/build-info.json`。

`VERSION_HANDOFF.md` 是 2026-08-29 的 **v14 审计**。其中 readonly、MCP、Provider、恢复等旧问题有的已在 v15 改动，不能整段复制为 v16 待办。历史 Downloads 中的 v15 方案本轮已无法读取，因此以当前代码和现有证据为准。

本轮执行了只读代码审计、Git/配置/证据核对及版本一致性检查；另做了无网络、无数据库、无真实文件操作的 Provider 内存探针。没有进行全量测试、真实模型调用、麦克风采集或安装验收。下文标为“建议目标”的数字均是未来验收标准，不是已达成结果。

## 二 版本目标与范围

### 用户应当获得的四个完整流程

1. **整理文件**：选择一个授权工作区，筛选一组文件，检查重命名或移动计划及冲突，确认后执行，看到逐项结果与恢复入口。
2. **修改文件**：读取受限大小的文本，查看差异；其他应用在预览后改过文件时停止，而不是覆盖新内容。
3. **中断后继续**：取消、关闭、Sidecar 崩溃后，重新打开能区分已完成、未执行和结果不确定的步骤，不重复删除或移动。
4. **选择可用的模型和输入方式**：知道当前模型适合对话还是文件工具；语音不可用时知道原因并可恢复，文字输入不被连带阻断。

### 已有能力继续沿用

- 持久化任务队列、租约和代次校验、SSE 事件、检查点、Verifier 与修复循环。
- 文件读写、patch、复制、移动、目录操作、50 项批次预检及进程内失败补偿。
- `.agent-backups` 中的可撤销备份及删除恢复记录。它是**司忆恢复区**，不是 Windows 系统回收站。
- DeepSeek、Ollama、OpenAI-compatible、ProviderDescriptor 与现有本地模型基准。
- MCP stdio/HTTP/Streamable HTTP 会话、授权、撤销及错误合同。
- 分层 Memory、人格身份、SQLite 迁移备份、Windows Credential Manager、统一构建身份。

v16 的增量是把这些能力连接成可信流程并补齐缺口，不再用“新增 Undo / 新增 Ollama / 新增多 Agent”等名称重复立项。

### 不纳入 v16.0.0 必选范围

- 新增全桌面应用控制、浏览器 Agent、剪贴板常驻监听、持续截图或后台录音。
- 手机/Web/PWA、云同步、服务端托管及跨设备账号系统。
- 更换数据库、重写整套 Runner、替换现有人格内核或建设另一套 Memory。
- 无人值守高风险自动执行、自动关闭其他应用、自动删除模型或备份。
- 以购买更大模型或静默切换云模型来掩盖合同错误；新增模型下载和付费调用另行确认。
- 自动更新器、代码签名服务采购、额外模型管理器等外延项目；可另列后续版本，不成为本版的范围增长点。

## 三 必须优先解决的具体问题

以下 P0 表示“本版不可带入正式发布的阻断项”，不表示已发生真实用户损失。源码缺口、实际复现和历史测试结果分开记录。

| 优先级 | 已核对的现状 | 影响及证据边界 | 核心位置 |
| --- | --- | --- | --- |
| P0 | 普通 `_undo_folder` 未对比 manifest 的 `after` 就删除当前目标并恢复旧内容 | 可能覆盖用户后续修改；UI 却提示遇冲突停止。调用链已核对，未在真实用户目录复现损失 | `siyi/app/sandbox.py:378`、`:1069`；`FileOperationsPanel.tsx:119` |
| P0 | `_rollback_backup` 在恢复异常后调用 `_discard_backup` | 恢复失败时可能删除仍有价值的备份；属于源码确认的危险分支 | `siyi/app/sandbox.py:353`、`:359` |
| P0 | 批次记录主要有开始/结束和失败回滚结果，执行进度及授权 proof 主要在进程内 | 未见完整的逐步崩溃恢复机制；不能将异常补偿等同跨进程事务。现有未知副作用默认 uncertain，并非已确认会盲目重放 | `siyi/app/tools/file_operations.py:83`、`:245`、`:283`；`batch_grants.py` |
| P0 | 本地测试覆盖率要求 80%，CI 默认继承 70%；CI 没跑 desktop/build-info 回归 | 新语音竞态测试仅挂到 desktop 脚本，可能不被当前 PR CI 检查 | `scripts/test.ps1:30`；`siyi/pyproject.toml:40`；`.github/workflows/ci.yml:42`、`:66` |
| P1 | 通用文件状态计算整文件 `read_bytes`，metadata 还可能全量解码和重复哈希 | 读取正文的大小限制不能保护所有 metadata、移动、删除、目录备份路径 | `siyi/app/sandbox.py:136`、`:499`、`:842` |
| P1 | Provider 的 structured_output 仅验证 JSON 对象，不验证传入 schema；stream_chat 关闭缺少子任务收尾 | 内存探针分别观察到不合 schema 的对象被接受、关闭迭代器后 chat 子任务仍存活。主聊天走另一条 completion 链路，不能称所有停止操作失效 | `siyi/app/providers/base.py:141`、`:183` |
| P1 | Ollama 探测到的上下文长度保存在 Provider 实例；Runtime 预算仍读取另一套配置/默认值 | 能力观测与预算存在断链；实际某模型是否溢出还需受控验证 | `providers/ollama.py:139`、`:268`；`providers/registry.py:43`；`runtime/model_loop.py:33`；`context/budget.py:57` |
| P1 | Worker 固定取队头，遇忙碌会话锁只等待再入队 | 可能让其他可运行会话被队头阻塞；源码确认分支，调度场景待测试锁定 | `siyi/app/runtime/task_runtime.py:284` |
| P1 | continuity 使用旧状态列表；MCP 发现未见 nextCursor 翻页 | 可能漏掉未完成任务或后续工具页；不应扩大成整个 Memory/MCP 不可用 | `memory/consolidation.py:38`；`tools/mcp.py:176`、`:194` |
| P1 | 现有文件 UI 以单文件表单和内存计划为主，目录/多步操作受限，结果偏原始 JSON | 工具已存在，但尚未成为完整的批量整理和中断恢复工作台 | `desktop/frontend/src/components/workspace/FileOperationsPanel.tsx:14`、`:76`、`:135` |

## 四 实施工作包

下列工作包全部为待实施。每项先补失败测试或 characterization test，再改实现；不得以删测试、降低权限或弱化验收解决失败。

### V16 B00 基线与证据归并

优先级 P0，所有实施的入口。

- 清点当前已修改/未跟踪文件，区分 9 月语音补丁、历史交接和新增 v16 文档；保存可恢复基线，不删除文件来获得 clean 状态。
- 将 v15 首次 DeepSeek FAIL、第二次 HTTP PASS、9 月 16 日便携修复分别纳入带来源的证据索引，保留失败历史和人工/模拟限制。
- 每条证据记录 requirement、测试类型、源码提交或指纹、build_id、schema、产物哈希、时间、适用范围；修正摘要靠重新计算，不手填 PASS。
- 校正文档过时索引，例如实际 Executor 为 `siyi/app/runtime/executor.py`，不是旧 `app/executor.py`。
- 决定旧候选生命周期：**建议在完成基线收敛后由 v16 接续开发，不强制先制造一个正式 v15 tag**。v15 未完成门禁保留历史状态，适用事项转为 v16 同类验收。若用户另要发行 v15，再分离补丁发布流程。

验收：任一“已通过”都能回答在哪一份源码/二进制上、以何种方式验证；旧证据不会自动变成 v16 证据。交付基线清单、证据适用性表及待办映射。

### V16 F01 冲突安全撤销与备份保全

优先级 P0，第一项产品改动。

- 统一单项、任务级和批次的恢复前置检查；复用 manifest `before/after`、路径保护、文件锁及已有 batch rollback proof。
- 现有目录状态主要是目录类型/mtime，不能证明子文件内容未变。目录恢复需有受扫描预算约束的子树身份和内容证据；旧 manifest 缺少 `after`、目录证据不完整或备份格式不明时，标为不确定/需处理，不能默认无冲突。
- 先按逆序模拟整个恢复计划及同一路径的依赖，再核验当前状态、备份完整性和全部涉及路径；不要把连续两次修改的两个历史 `after` 都直接与当前文件比较。每一步真正执行前还须重新校验路径、文件身份、版本和备份；不能只做一次预检。
- 明确提交阶段的并发保护与不能安全恢复时的拒绝边界。进程内文件锁不能约束其他应用；预检后出现外部修改、新内容或备份损坏时停止，不强制覆盖，不宣称普通预检消除了所有竞争窗口。
- 恢复途中失败保留备份与逐项结果，进入需处理状态；禁止捕获异常后无条件删除唯一副本。
- 内部执行失败补偿和用户主动 Undo 共用安全底层，但必须区分未完成 after 记录、不确定结果及授权来源，不能机械共用一个放宽检查的开关。
- 文件面板显示可恢复范围、冲突路径和失败原因；不把“存在备份”显示成“保证可恢复”。

验收：外部编辑后 Undo、新目录中新增文件后 Undo、移动目录后其已有子文件被修改、同一路径连续修改后的整任务逆序撤销、文件被替换成目录、缺失/篡改备份、锁文件、磁盘写失败、第二项恢复失败全部覆盖。另在“预检后”和“第一项恢复后”注入外部编辑；新内容不丢失，失败备份不消失，未完成恢复不显示成功。测试写入仅限隔离目录。

### V16 F02 可持久恢复的文件批次

优先级 P0，依赖 B00 和 F01。

- 在现有 `file_transactions`、`rollback_records` 及文件备份记录之上增加稳定 operation_id、调用来源和步骤日志，不新建竞争性的文件执行器。
- 将 UI 单文件、Agent tool、batch 入口收敛到同一 Executor/receipt/锁/审计边界，保持旧 tool 名称兼容；不要把现有入口差异未经验证地称作权限旁路。
- 逐项记录准备、已验证备份、开始副作用、已观察结果、提交/补偿结果；状态名称在接口设计时统一，不同时维护多套枚举。
- 文件系统与 SQLite 无法靠一次普通数据库提交实现共同原子性。针对“文件已变但 journal 未提交”的窗口做现场核对；不能证明结果时标 uncertain/needs_attention，由用户决定下一步，不自动重放。
- 计划绑定会话、工作区、操作摘要及版本；预览或文件变动使批准失效。恢复不能扩张或重新复用过期授权。
- 首版保留 50 项批次上限；复杂目录和跨卷操作先明确可支持范围，不用扩大上限掩盖恢复成本。

验收：对 1、10、50 项混合批次，在每类“副作用前后、日志提交前后、补偿前后”注入进程中止；重启后逐项可解释，不重复移动/删除，不假报整批完成。取消在安全点停止，不回放已确认步骤。外部命令和远程 MCP 仍按 receipt-only 边界展示，不承诺全局撤销。

### V16 F03 大文件与 Windows 资源边界

优先级 P1，依赖 F01 合同，可与 F02 部分并行。

- 将通用 hash/metadata 改为有界分块处理；复用已有分块版本扫描，不让 metadata 全量解码正文或重复扫描。
- 保留文本读取/编辑大小上限；区分“不能全文编辑”与“不能列出或移动”。未知类型明确展示，不把二进制强行按文本读取。
- 目录扫描、备份和批次预检支持条目/总字节/磁盘空间/时间预算与取消；检查时间和占用原因可见。
- 复核中文、空格、大小写、长路径、锁文件、junction/reparse、跨卷情况；仍拒绝既有禁止路径，不为支持大文件绕过沙箱。
- 司忆恢复区增加分页与容量可见性；默认不自动清理恢复资料。清理策略必须另有明确选择与影响说明。

建议验收预算：对 1 GiB 合成文件做 metadata/hash，额外 RSS 不随文件体积线性增长，初始目标不超过 64 MiB；可取消扫描在 2 秒内结束等待或返回明确不可中断阶段。性能指标固定测试机及缓存状态，5 次采样报告中位数和最差值；达不到时记录原因，不改写测试数据。另覆盖磁盘不足和备份部分写入失败。

### V16 U01 文件任务工作台

优先级 P1，产品主交付，依赖 F01/F02/F03 的接口。

扩展现有 `FilesPanel` 和 `FileOperationsPanel`，不另建文件管理器或改造整套视觉系统。

- 增加多选、过滤和范围摘要；首批完整支持批量改名、分类移动、受限文本修改、恢复最近批次四类流程。
- 计划展示来源/目标、文本 diff、影响数量、预计备份量、风险与可恢复程度；普通结果改为可读状态，原始 JSON 留在高级诊断。
- 冲突首选停止；可明确选择跳过或换名，再重新预检。**自动覆盖和永久删除不作为默认冲突策略。**
- 计划和执行状态以服务端事务为准，刷新/重启可重连；不把 React 内存数组当持久事实。
- 会话、工作区、权限切换后旧计划不能提交到新上下文。批次中同一路径的多步依赖只在后端明确支持时开放。
- 恢复区显示仍可恢复、冲突、部分恢复、已恢复等真实状态；需人工处理的项不隐藏。

验收：每类流程贯通“准备→预检→确认→执行→核验→恢复”；覆盖只读/ask/agent/full、拒绝、双击提交、网络响应晚到、窗口重启和外部修改。只读模式不能通过确认解除禁写。执行前拒绝不得开始副作用；运行中收到取消后不再启动新的正向步骤，但已进入不可中断提交阶段的步骤可能完成，且仅允许权限合同明确覆盖的本批次安全补偿。界面必须显示实际结果，不承诺点击取消即恢复原状。补测取消发生在提交/补偿窗口及中途切换 readonly：取消不能赋予新写权限，无合法恢复授权则保留现场并进入需处理状态。

### V16 P01 模型能力与预算统一

优先级 P1，保障文件计划及模型调用的正确性。

- 保留 ProviderDescriptor 与注册表，引入一个供设置页、路由、预算和 benchmark 共享的“当前配置下有效能力”解析入口。
- 能力键必须区分 provider、规范化端点身份、model、digest/版本及配置。分开记录声明、实际观测和人工设置；未知不能默认成支持，不能只用模型名跨端点复用。
- 将 Ollama 实际加载配置/观测窗口接入 Runtime 预算。模型理论窗口、运行时配置窗口和用户请求是不同值；不得因当前 8192 下限或 65536 fallback 而放大更小的实际窗口。超预算先明确压缩/拒绝，不静默替换用户选定模型。
- structured_output 对传入 schema 做本地验证；对不支持的 schema 特性明确拒绝或标示，不仅检查 JSON 能否解析。修复次数、时间、Token 均有界。
- Provider stream 在消费者取消/关闭时取消并 await 自有子任务，传播错误并清理；同时保留主 Runtime 现有 cancellation 机制，分别测两条链路。
- 将 Basic 对话、只读 Tool、结构化计划、文件执行分别显示资格。复用现有 benchmark，并新增隔离 NTFS 工作区上的真实 Runtime 文件场景；保留旧内存模拟测试的名称与限制。
- 模型能力不足时明确限制对应入口，不能偷偷切云、降低权限或让模型自身拒绝承担内核安全责任。

验收：跨端点同名模型不串能力；小上下文模型预算不超实际配置；错误 schema 被稳定拒绝；关闭流后无悬挂自有任务；限流/余额不足/超时/空可见输出分开处理。拟标记为“支持文件任务”的模型须对必选场景各运行至少 3 次并逐项通过；失败模型可保留对话功能，不能贴上文件 Agent 合格标签。指定默认文件 Agent 模型未通过时，该产品能力仍为 BLOCKED，不能用隐藏标签规避整体验收。

### V16 R01 调度及外围合同收敛

优先级 P1，以小改和特征测试为主，不重写 Runtime、Memory 或 MCP。

- 为队头会话锁忙而其他会话可运行的场景补测试；调度应在保持同会话串行和优先级的前提下选择可运行项，避免整体空转。
- continuity、恢复面板等消费者引用权威 TaskStatus 分类，覆盖 queued、waiting_tool、waiting_provider_credential、recovering 等状态；保留个人与项目 Memory 的命名空间和写入策略。
- MCP tools/list 增加有界分页、重复 cursor 防护、重复名称冲突处理、总量/字节/时间限制；刷新或撤销立即影响路由权威，分页不能绕过原权限与网络边界。
- 在测试保护下继续抽取 Runner 内聚职责；不以文件行数为唯一目标，不删除兼容表和旧 API。

验收：两个会话、至少两个 worker，A 队头被锁时 B 在约定调度窗口内开始，同会话无并行副作用；所有 TaskStatus 的快照归类有穷尽测试。MCP 3 页发现、重复 cursor、半途失败、schema 异常和中途撤销均有确定结果；失败不能把半页工具误标成完整可用清单。

### V16 V01 语音与资源状态完成闭环

优先级 P1，沿用已有 STT/TTS 与 9 月修复。

- 将模型缺失、首次加载、设备权限、静音/无输入、设备拔出、转写、失败、取消、可重试统一成可观察状态，不重新写整个 `useVoiceCapture`。
- 资源检查展示本次可用 RAM/安全下限和采样时间；未知就是未知，不能显示成 0 或保留上次成功状态。保留默认 **2 GiB** 门槛，不使用 override 冒充正式验收。
- Ollama 不在线时仍能诊断 CPU STT；设置面板各服务失败分别显示，不把拉取失败统一伪装成“未安装”。
- 完成实际 Windows 麦克风固定非敏感短句→停止→small 转写→草稿回填→用户确认→Agent 响应。设置测试录音不应意外发送聊天。
- 失败与取消不遗留音频、mic track、AudioContext、资源预留或失去所有权的 worker；用户可释放资源后直接重试。

验收分四层：模拟 Hook 竞态、冻结依赖加载、真实本地模型转写、真实麦克风桌面流。四层分别记录，不互相替代。建议真机至少连续 3 次正常短句，并覆盖取消、失败后重试与退出清理；当下麦克风使用由用户确认，不后台录音。低内存负例只使用隔离注入，不制造系统内存耗尽。

### V16 Q01 一致门禁与最终桌面交付

优先级 P0，B00 后即开始，贯穿全部里程碑。

- 本地、PR CI、RC 共用门禁定义；采用现有本地 **80%** 覆盖率要求作为统一下限，不将 70% 路径当等价验证。关键安全/恢复场景须逐项通过，覆盖率数字不能替代。
- CI 明确执行 lint、类型/构建、security、desktop、voice-errors 和 build-info 的有效入口，允许组合脚本避免重复；验证“故意破坏竞态/构建身份”会真正导致 CI 失败。
- 修复 LF/CRLF 敏感静态断言；新 Windows checkout 无需人为制造历史路径 sentinel 才能跑测试。测试夹具应由测试自身建立并标为非发布证据。
- 门禁与 benchmark requirement ID 按目标版本/协议版本处理；例如当前 `V150-LOCAL-MODEL-*` 不能无校验复用到 v16。保留历史报告可读，不全局替换旧证据。
- 当前 `release-gate.ps1` 主要检查 Eval 报告，不是安装、人工、分发的总门禁；增加或收敛总编排，明确各检查职责。
- RC 总门禁校验预定 suite/case 清单、report mode/layer、证据归属与比较基线，拒绝缺项或用不相符层级报告替代。scripted 仅证明确定性运行时；默认模型的真实 Agent 场景仍需真实证据。当前 `siyi/app/evals/comparison.py:115` 的聚合不能单独承担这些校验，且 release-gate 的 Baseline 可省略；v16 总编排必须显式补上。
- “无回归”必须附同合同、同模式及可比较环境的基线；不兼容或缺失基线时先标为未验证并建立基线，不能因未传 Baseline 就判为无回归。云模型场景另需预算授权，不自动触发。
- 保留同一 manifest 派生 React/Tauri/Sidecar 身份、干净正式源码、构建指纹、SBOM、迁移前备份和受管进程身份检查。
- 最终 NSIS/MSI 验证安装、启动、退出、升级保数、卸载保留策略、重新安装、组件错配及恢复路径。快捷方式/启动入口必须指向验收过的程序，不只核对标题版本。
- 升级基线分别记录“可确认的上一正式安装版”和“当前 v15 本地便携候选数据”。未找到正式 v15 包时不能虚构一个作为稳定基线；允许直接从真实上一正式版本验证升级。

验收：当前候选的源码/二进制/证据一致；旧 Sidecar、错 schema、错 hash、正式 DIRTY 包、错误证据归属及自动化冒充人工均被拒绝。签名、发布、远端上传的状态如实报告，不能把本地构建成功写成正式发行。

## 五 架构与数据设计边界

| 保留 | 局部调整 | 不做 |
| --- | --- | --- |
| Tauri 管理 FastAPI Sidecar 的启动及身份验证 | 异常恢复与旧进程/新包共存测试 | 替换为新 Desktop 框架 |
| SQLite 权威队列、租约、检查点、事件 | 文件步骤日志、稳定操作 ID、状态消费者统一 | 内存队列取代 SQLite |
| Permission Broker、Executor、路径保护、审计、Verifier | 各文件入口共用执行与恢复合同 | 前端单独授权或 UI 自行写文件 |
| 现有备份格式与恢复资料 | 格式版本、校验、部分恢复状态及兼容读取 | 自动删除旧备份/历史表 |
| ProviderDescriptor、Context assembler/budget、MCP session | 有效能力解析、schema 校验、取消、分页 | 每类模型再复制一份预算或权限体系 |
| 人格身份与分层 Memory | 任务状态一致性和可解释引用 | 自动扩大记忆范围或无授权存个人经历 |

若 F02 需要 schema 迁移，应在设计冻结时分配下一空闲迁移号，不因产品版本是 16 就强行改 schema 为 46 或 16。必须先备份，支持失败恢复；新增字段向后兼容，暂不删除旧表。

回退程序不等于回退数据。若新 schema 不能被旧程序读取，回滚应恢复与旧程序匹配的备份副本，先保留新数据；不得让旧程序直接写入不认识的新 schema。

## 六 实施顺序与并行安排

| 里程碑 | 交付内容 | 进入下一阶段的条件 |
| --- | --- | --- |
| M0 基线冻结 | B00 归并、Q01 门禁合同、待办和风险测试清单 | 未提交变更归属清楚，证据适用性明确，不先改 VERSION |
| M1 文件数据安全 | F01；F02 的稳定 ID、日志与恢复设计；Q01 CI 补齐 | 撤销不会覆盖外部变化；失败保留备份；相关失败测试转绿 |
| M2 执行与资源可靠性 | F02、F03；P01、R01 可按模块并行 | 中断恢复、资源有界、Provider/调度合同通过集成检查 |
| M3 用户工作流 | U01、V01、P01 能力分级及真实 Runtime 基准 | 四个目标流程贯通；需要真机/付费的项有明确待验收状态 |
| M4 冻结候选 | 全量门禁、最终打包、真机和安装升级验证 | 技术与人工必需门禁通过，无数据安全 P0 未解 |
| M5 正式发行 | 经授权完成 final tag、workflow、上传及下载后安装复核 | 最终来源、哈希和发行证据一致，才可标记 RELEASED |

推荐三条并行开发线：文件与恢复（F01/F02/F03）、模型与运行时（P01/R01）、桌面体验与交付（U01/V01/Q01）。文件 UI 的原型/契约测试可先做，但执行按钮接入等待恢复接口稳定。

`sandbox.py`、`runtime/runner.py`、数据库迁移、`types.ts` 和 `package.json` 由单一集成人统筹修改，避免多代理同时改同一合同。子任务按明确文件范围分工，每个交付附 diff、测试命令、结果和剩余风险；代码作者不独自签署最终验证。

不承诺未经估算的自然日交期。M0 先对每包按依赖和测试成本估算；范围压缩时先延期外延能力和非关键 UI 优化，不能删除恢复、权限、真实能力或发行门禁。

## 七 验收标准与证据层级

### 统一的发布要求

1. 全部 P0 完成；未经处理的数据损失、越权、盲目重放、错误来源证据均阻断发布。
2. 全量 Python 测试和统一 ≥80% 覆盖率、前端 lint/build/security/desktop/build-info、Rust tests 通过。跳过项必须解释环境与风险，必需场景不能靠 skip 算通过。
3. 核心、Multi-Agent、Professional、Adversarial 的相关完整 Eval 通过，且 suite/case 清单、mode/layer 与预定验收一致。沿用现有 `evals/gate-policy.json` 至少 85% 成功率、零虚假成功、零无关文件修改、零权限/沙箱违规和无回归要求；必选场景须单项通过，不能被平均值覆盖。无回归必须有同合同的可比较基线；无基线时为未验证，而非默认通过。scripted 结果不能代替默认模型的真实 Agent 场景。
4. Undo、批次中断恢复、权限模式及版本冲突的必选矩阵全通过；模型拒绝率和内存模拟不得代替真实 Runtime/Permission Kernel 检查。
5. 正式宣称支持的默认模型与语音能力有对应真实证据；资源不满足时负例正确，不为拿到绿灯降低内存安全线。
6. 最终候选完成隔离安装、升级、数据库备份恢复、卸载保留、退出清理，以及来源/哈希复核。
7. 性能与同机同路径基线比较，至少 5 次采样。建议启动中位数回归超过 20% 阻断、超过 10% 需说明；资源诊断与 UI 反馈分别测量，不用最快一次代表总体。

### 检查分层

| 层级 | 何时执行 | 证明什么 | 不证明什么 |
| --- | --- | --- | --- |
| 定向回归 | 每次小改 | 修改合同、负例及取消清理 | 整个产品已可发布 |
| 全量及故障注入 | 每个里程碑 | 模块集成、迁移、恢复和安全矩阵 | 当前安装包已包含改动 |
| 冻结程序烟测 | 每份 RC | 原生依赖、组件身份、进程生命周期 | 用户真实录音或云服务成功 |
| 真实模型与桌面 | RC 人工验收 | 指定模型/设备/工作流实际表现 | 别的模型或旧/新构建同样通过 |
| 发行后复核 | 正式发布后 | 下载物身份、安装和真实入口 | 未来长期稳定性 |

证据状态至少区分 PASS、FAIL、BLOCKED、NOT_RUN、INELIGIBLE、STALE。版本相同不代表源码相同；后续功能改动应按影响重新验证。能否复用某项证据需说明依据，不能机械清空全部历史，也不能全盘继承。

发布顺序应避免循环依赖：先冻结源码并通过不依赖最终 tag 的技术/人工候选检查，再按现有 source/evidence 提交规则绑定证据并经授权建立 tag，随后完成 tag 一致性、workflow、上传及下载复核。不要要求“包含 tag 的所有门禁先 PASS，才允许创建 tag”。

建议后续交付文件仍放在 `docs/16.0.0/`：需求/验收映射、TEST_MATRIX、RELEASE_STATUS、EVIDENCE_MANIFEST、IMPLEMENTATION_FEEDBACK、MODEL_BENCHMARK 与 RELEASE_NOTES；原始日志和隔离测试产物放在忽略的 `build/v1600-evidence/`。本轮没有创建这些完成性报告，以免误认已实施。

## 八 授权与用户参与边界

普通实现、单元测试和合成临时数据验证可在获得后续实施指令后执行。以下事项不能从“规划 v16”推导为已授权：

- 新的付费 Provider 调用。历史两次是有限许可；执行时确认次数、预算和所验证的场景，禁用自动重试扩大消费。
- 实际麦克风采集。执行前说明固定非敏感短句、时长和音频去向，由用户准备后开始。
- 新模型下载、删除已装模型、备份清理、真实用户文件批量修改。
- UAC/管理员安装、关闭其他应用、改系统环境。
- commit/push、正式 tag、远端发布和不可变产物替换；本方案不执行这些动作。

这些边界应在 M0 就列为检查点，不等开发结束才暴露。不可获得必要授权时保留 BLOCKED，不偷偷使用已有凭据或模拟结果替代。

## 九 关键源码与测试索引

| 工作包 | 主要现有源码 | 可复用验证 |
| --- | --- | --- |
| F01/F03 | `siyi/app/sandbox.py`、`siyi/app/workspace/file_locks.py`、`siyi/app/workspace/snapshots.py` | `tests/backend/tools/test_sandbox.py`、`tests/backend/tools/test_file_operations_v920.py` |
| F02 | `siyi/app/tools/file_operations.py`、`siyi/app/tools/batch_plan.py`、`siyi/app/tools/batch_grants.py`、`siyi/app/runtime/recovery_policy.py`、`siyi/app/database_modules/` | `tests/backend/tools/test_file_batch_v150.py`、`tests/backend/runtime/test_recovery.py`、`tests/backend/runtime/test_startup_recovery_v150.py` |
| U01 | `desktop/frontend/src/components/workspace/FilesPanel.tsx`、`desktop/frontend/src/components/workspace/FileOperationsPanel.tsx`、`desktop/frontend/src/shared/fileOperationPolicy.ts`、`desktop/frontend/src/components/tasks/TaskExecutionBlock.tsx` | `desktop/frontend/scripts/file-operations.test.ts`、`desktop/frontend/scripts/desktop-components.test.mjs` |
| P01 | `siyi/app/providers/base.py`、`siyi/app/providers/registry.py`、`siyi/app/providers/descriptors.py`、`siyi/app/providers/ollama.py`、`siyi/app/context/budget.py`、`siyi/app/runtime/model_loop.py`、`siyi/app/evals/local_model_benchmark/` | `tests/backend/providers/test_provider_registry.py`、`tests/backend/providers/test_provider_v2.py`、`tests/backend/providers/test_provider_contract_v910.py`、本地模型测试 |
| R01 | `siyi/app/runtime/task_runtime.py`、`siyi/app/runtime/queue_service.py`、`siyi/app/runtime/task_state.py`、`siyi/app/memory/consolidation.py`、`siyi/app/tools/mcp.py`、`siyi/app/mcp/` | Runtime 队列/恢复测试、Memory 测试、`tests/backend/tools/test_mcp_contract_v150.py`、`tests/backend/tools/test_mcp_lifecycle_v150.py` |
| V01 | `siyi/app/voice/session_manager.py`、`siyi/app/stt/manager.py`、`siyi/app/local_runtime/resource_coordinator.py`、`siyi/app/api/routes/local_models.py`、`desktop/frontend/src/hooks/useVoiceCapture.ts`、`desktop/frontend/src/voiceErrorPolicy.ts` | 9 月新增后端 STT/voice/local_models 测试、`desktop/frontend/scripts/voice-error-policy.test.ts`、`desktop/frontend/scripts/voice-capture-errors.test.mjs` |
| B00/Q01 | `scripts/check-release-metadata.py`、`scripts/generate_build_info.py`、`scripts/check-release-evidence.py`、`scripts/test.ps1`、`scripts/build-desktop.ps1`、`.github/workflows/` | 元数据/构建身份、冻结 Sidecar、NSIS/MSI、迁移、隐私及 Eval gates |

表中路径均以仓库为根。具体函数可能在实施中移动，以契约和测试保持兼容。

## 十 给后续开发 AI 的执行摘要

你接手的是司忆 Windows 个人桌面 Agent，不是金融项目，也不是要重新开发一个 Agent 框架。当前源码是 v15.0.0，HEAD `7449c43` 之外还有已交付便携程序的未提交语音补丁。先读取 AGENTS.md、当前 status/diff 和本方案，保存用户修改，不清理目录、不先提升版本号。

第一阶段不是增加更多工具，而是复现并修复普通 Undo 不校验外部改动、恢复失败删除备份，以及批次缺少逐步持久恢复的问题。复用已有 Executor、文件备份、权限、审计和 SQLite。文件系统和 DB 的非原子间隙必须显式核对；不确定副作用不能自动重放。

然后使文件 metadata/备份扫描资源有界，完成现有 Files UI 的多选计划、冲突反馈、逐项进度和恢复。Provider 抽象、Ollama、MCP 和分层 Memory 已经存在；只修能力/预算断链、schema 校验、流取消、分页、状态消费者及调度公平性，不再给它们另起一套框架。

v15 的 28/31 门禁摘要已经过时：第二次 DeepSeek HTTP 请求有 PASS，但属于旧提交和最小直连；真实麦克风闭环仍待验收。9 月 16 日的 85 项定向测试也不能代替当前全量或 v16 发布证据。统一 CI 与本地门禁，保留所有失败历史和测试资格限制。

完成每个工作包后提交范围清楚的结果和证据，由独立复核检查；只有最终候选、真实能力和发行链全部满足要求，才称 v16.0.0 已发布。若需裁剪，裁剪非必要外延，不裁剪数据安全、默认承诺能力和可恢复性。
