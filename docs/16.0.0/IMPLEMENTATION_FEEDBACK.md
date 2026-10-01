# 司忆 v16.0.0 实施反馈与验收交接

本报告更新旧 rc9 交接结论；旧原始日志、JSON、JUnit 和安装包保持不变。当前正式发行状态：**BLOCKED**，不代表停止执行。

## 1. 当前范围与授权

- 项目仅为司忆 Windows PC Agent，源码版本16.0.0，schema46；不转向基金或其他仓库。
- 用户已明确授予本版本剩余范围内操作权限：管理员验收、本地模型、最小付费验证、设备验收、安装、本地源码checkpoint及后续发行步骤。不再等待重复授权。
- 权限不替代资源、安全或真实验收；不会降低2GiB语音门槛、自动关闭用户应用、读取真实用户数据库或把Mock当模型资格。
- 原22项用户改动和基线备份保留。原 VERSION_HANDOFF.md 内容/哈希不变，通过本地 .git/info/exclude 排除发布输入，不删除、不静默脱敏。
- 根目录司忆.exe、agent-backend.exe和_internal仍v15，哈希与原基线相同；暂未覆盖、安装或迁移真实用户数据。

## 2. 本轮实际推进

| 检查 | 实际结果 | 边界 |
|---|---|---|
| 管理员符号链接六案例 | 6/6 PASS，无跳过 | 隔离fixture；未开启Developer Mode |
| 管理员全量r6 | 1909 PASS / 3 FAIL / 21 SKIP | 旧447A源码；3失败为PowerShell中文路径stdout乱码；不是最终修复后证据 |
| 中文路径CP936回归 | 14/14 PASS | 子进程显式UTF-8，不修改系统代码页 |
| 最小DeepSeek调用 | 1次、零重试、16输出Token上限；17输入+3输出=20Token、722ms | 固定公开短提示；独立保守费用估值约束；不是Agent完整模型资格 |
| 费用修复最终定向 | 218 PASS，1既有弃用警告 | 最终取消持久化失败、同代熔断、namespace/续期/4096容量均通过；全Mock、无付费 |
| 独立费用反例组 | r4 7/7 PASS | 最后取消数据库不可写漏洞已先复现RED再修复GREEN；无新付费 |
| 费用前端生产组件 | 12场景PASS | 未知/partial/pending不显示零，真零正常显示，微小正值不四舍五入成免费 |
| RC独立pytest临时目录 | 15/15 PASS | 避免管理员/非管理员共用默认pytest-temp的ACL冲突 |
| 本地模型/语音资源预检 | BLOCKED | RAM1147224064B、空闲VRAM4582277120B，低于本轮保守加载余量；未加载/录音 |

不可把重叠定向测试数量相加为全项目独立用例数量。实际原始路径分别见 TEST_MATRIX.json 和 EVIDENCE_MANIFEST.json；本报告不回写旧结果为成功。

## 3. 新发现P1费用问题与修复

最小真实调用发现：未配置价格时，旧后端费用估值和SQLite兼容列为0，前端也显示0，美元预算可能永远不耗尽。实际usage确认为20Token，但价格未知，费用应是未知而不是免费。

已实现主要链路：

- 冻结Provider/端点hash/模型价格快照；拒绝缺项、布尔、负数、NaN、Infinity，不自动写官方价格。
- DTO统一 cost_status、nullable estimated_cost_usd、known_cost_usd、unknown_cost_requests、pending_cost_requests。
- 根任务租约下BEGIN IMMEDIATE原子预算预留；model_run写入与结算同一事务。
- 正美元预算禁用隐含重试；所有Planner/子Agent/压缩/Verifier/修复阶段共享根任务，未知结算停止后续工具调用。
- 自动标题使用本地fallback，避免预算外隐含付费。
- 前端费用未知不填零；真实零费率仍可确认零费用。

最终复核发现的取消后持久化失败/日志遮错/兄弟继续漏洞已修复：成本安全异常保持优先，同数据库/根任务/generation进程熔断作为写库失败后备。2个故障反例先RED再GREEN，最终218项组合通过；源码现在冻结，须跑新CLEAN全量门禁。

详细合同见 COST_SAFETY.md。支出门禁依赖输入Token估值和显式用户价格，**不是服务商账单硬上界**。schema仍46；历史库缺agent_tasks.price_snapshot_json时正美元预算安全阻断，未自动加DDL。

## 4. 已完成的v16主要能力

- 文件：Windows句柄相对reparse安全提交、全逆序撤销预检、逐步身份/内容/权限复核、耐崩溃SQLite journal、只读reconcile、不确定副作用不重放。
- 资源：1MiB累计分块预算、超时/取消/容量未知fail-closed、恢复清单分页、64KiB编码探针。
- 工作台：复用现有文件面板，最多50文件选择、批量重命名/分类/字面替换、diff与冻结身份、迟到响应隔离及恢复入口。
- 模型：能力身份/TTL、显式窗口/小输出预算、有限schema、流取消、四级本地资格导入；scripted结果不授予真实资格。
- 运行时：跨会话公平、同会话串行、23任务状态、MCP有界分页和原子租约发布。
- 语音：原2GiB保护、资源未知语义、服务诊断与取消清理；Windows TTS中文stdin的UTF-8/936问题已修复。

以上源码已集成，不等于所有真实模型/设备/安装流程都已验收。

## 5. 历史rc9，不能继承为当前源码

旧候选build_id ac4cb78e8db86ffe561519cb，指纹447A055A2516B70DF40590C4A5CA0E6C1B4B5B8F36B5CDB8677A341D181CF04B，来源DIRTY：

- 后端r5为1906PASS/27SKIP，83.4827%覆盖；严格wrapper因mandatory symlink skip失败。
- Rust17PASS，四组scripted Eval40/40，scripted文件Runtime30/30；所有真实本地模型资格false。
- 实际1GiB NTFS额外RSS2248704B、五样本中位1139.54ms；非旧版配对。
- Desktop五样本中位2690ms，实际React/Tauri/认证Sidecar身份收据及主HWND关闭成功；非人工工作流。
- NSIS/MSI仅构建；MSI249文件静态提取、精确SDK3字节派生hash和隔离启动；非真实安装。
- 冻结Sidecar文档格式和TTS/VAD依赖探针通过；small语义转写被3GiB预检阻断，未录制麦克风。

费用/编码修复和本地隐私排除改变源码来源；须新checkpoint、全量证据、新包，不能把旧DIRTY修改成CLEAN。

## 6. 自动继续顺序与剩余真实门禁

1. 取消费用链已修复并独立验证 → 隐私/差异审核 → 范围明确的本地CLEAN checkpoint。
2. 管理员完整后端r7，当前源码的前端/Rust/脚本Eval回归；不改测试预算换取PASS。
3. 构建新的CLEAN候选并核验冻结Sidecar、三组件启动、完整payload和installer来源。
4. 精确安装碰撞检查、合成数据fixture与备份后做真实安装/升级/卸载；实际主窗和nonce三组件收据不能用弱CloseMainWindow替代。
5. 已选已安装qwen3:4b、目标16384窗口；资源足够时运行local_live，不下载或改默认Provider。真实麦克风也等资源安全门槛满足；不是缺授权。
6. 费用修复验证后，固定有限预算验证默认云模型Agent；最小HTTP成功不授予文件Agent资格。
7. 补实际界面工作流和可比基线；预tag门禁就绪后再上传/tag/下载复核及完整备份便携替换。

当前尚无本版本正式tag、远端发行、真实用户库迁移或根portable替换。无需重写Agent内核、Executor、权限、Memory或现有文件面板；继续填真实证据，不扩大产品范围。
