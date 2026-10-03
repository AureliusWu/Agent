> PUBLICATION NOTICE / 公开脱敏展示说明：本文件来自历史 v16 候选提交 `129d16b87fcdea42c319a693ece438d9bf09ae9b` 对应的未提交证据备份，仅机械替换本机绝对 Windows 路径；状态、FAIL、门禁和数值未被升级。本轮源代码合并不等于重新验收或正式发布。原始文件字节保存在私有本地备份，原始 SHA-256 为 `65A3147F7DAAE6BEF10277D088CFCA763C9BD1451FCE2F6CD59E4570FCE223D4`。正文中的原制品或原文档 SHA 仅适用于私有原件，不能用于验证本公开展示副本。
# 司忆 v16.0.0 实施反馈与验收交接

本报告更新旧 rc9 交接结论；旧原始日志、JSON、JUnit 和安装包保持不变。当前正式发行状态：**BLOCKED**，不代表停止执行。

本地源码checkpoint已完成：HEAD `129d16b87fcdea42c319a693ece438d9bf09ae9b`，源码指纹 `B71992C39D775F7427009A16289B1D28FB809004FBF5C47A12A562E3274B219A`，发布源码状态CLEAN。父checkpoint8b1/F8保留，本次仅修路径测试合同，不改生产逻辑。仅六份明确排除源码指纹的生成验收资料随后更新；原始git工作区可能显示这些文档变更，不伪称所有文件无差异。尚未push/tag。

管理员新全质量r7已中断，进程不再存在；日志在约64%停止，无最终收据/JUnit/coverage，退出原因未确认，不能称全量通过。第633项可见失败已定向定位：旧测试断言遗漏生产证据展示函数的合法仓库相对路径分支。测试合同已最小修复，双布局各24PASS，原始失败保留。fresh管理员完整质量r8已实际完成：1990PASS/21SKIP/0FAIL，覆盖率83.73579017757635%，Rust17及前端lint/build/security/desktop全部PASS，19项自动门禁true；proof为build/v1600-evidence/full-backend-20261002-v16-r8-admin.json，独立envelope为build/v1600-evidence/admin-full-backend-v16-r8-envelope.json。未运行安装、真实Agent资格或麦克风；rc11候选已构建且portable冻结runtime PASS_RUNTIME_ONLY；NSIS/MSI已实际生成BUILT_NOT_INSTALLED_NOT_RELEASED；未安装、未发行，静态no-WebView已核对但完整运行网络未验收、未签名。

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
| 历史最小DeepSeek调用 | 1次、零重试、16输出Token上限；17输入+3输出=20Token、722ms | 旧生产单价缺失，旧存储0仍是unknown_not_free；不是Agent资格 |
| 当前CLEAN r8全质量 | 1990PASS/21SKIP/0FAIL，覆盖率83.73579017757635%；Rust17和前端门禁PASS | 19项自动门禁通过；不替代安装、实际界面或模型资格 |
| 当前成本链路付费smoke | 1次HTTP transport、零重试；17输入+3输出=20Token；known估值USD0.00003432 | 生产根task/lease/reserve/同事务settle/check_result各链路通过，pending0；本次显式费率非官方或账单价 |
| fresh当前scripted r2 | core18/18、multi10/10、professional8/8、adversarial4/4；文件Runtime30/30 | 合成隔离合同；四级模型资格false，独立network/paid counter未记录 |
| 候选构建rc10/rc11 | rc10 FAIL exit1；rc11构建exit0、portable runtime PASS_RUNTIME_ONLY | rc10失败保留；rc11三份artifact hash及实际runtime核对，未安装 |
| 当前portable冻结runtime | 8执行exit0，Job owned descendants0；6桌面收据通过 | Sidecar文档reopen/render、Windows中文TTS/nativeVAD探针通过，不加载STT；不是人工/模型/安装验收 |
| 当前冻结合成STT | small CPU/int8 DEVELOPMENT_PASS；公开中文归一化转写一致、similarity1.0、5251ms | 原2GiB保护/本轮3GiB余量不变，既有4模型文件仅复制、原hash未变；无下载/付费/真实麦克风或LLM资格 |
| 当前桌面启动计时 | 5样本[3002,2641,3162,3094,2612]ms；中位3002、最差3162 | portable warm路径，实际React/Tauri/认证Sidecar nonce/PID/mainHWND身份及关闭核对；未配对旧版，不能称无回归 |
| 官方8.0.1升级基线 | NSIS/MSI digest与字节核对；只读MSI UpgradeCode匹配 | 仅下载，未安装/执行；真实升级NOT_RUN |
| 费用修复最终定向 | 218 PASS，1既有弃用警告 | 最终取消持久化失败、同代熔断、namespace/续期/4096容量均通过；全Mock、无付费 |
| 独立费用反例组 | r4 7/7 PASS | 最后取消数据库不可写漏洞已先复现RED再修复GREEN；无新付费 |
| 费用前端生产组件 | 12场景PASS | 未知/partial/pending不显示零，真零正常显示，微小正值不四舍五入成免费 |
| RC独立pytest临时目录 | 15/15 PASS | 避免管理员/非管理员共用默认pytest-temp的ACL冲突 |
| r7失败双布局复现 | 仓库内20PASS/1FAIL；系统temp21PASS | 同源码，明确为旧路径断言与合法RC临时布局不兼容；非真实STT验收 |
| CLEAN源码1GiB文件metadata | PASS，额外RSS2449408B、中位1112.73ms | 8b1/F8源码；合成文件、非桌面启动或配对旧版基线 |
| 路径测试修复双布局 | 每组24PASS，0跳过 | 新增repo/LOCALAPPDATA/external三分支严格脱敏合同；不改变生产显示逻辑 |
| 本地模型/语音资源预检 | r2历史READY及r3 BLOCKED保留；最新owned-qwen r1真实预检BLOCKED | 2026-10-02T03:20:18.453290Z RAM4983595008B已过3GiB、VRAM5264900096B低于5368709120B；初始门槛拒绝，Job exit/cleanup0，无clone/服务/模型/CLI；不授予Basic/默认模型资格 |

不可把重叠定向测试数量相加为全项目独立用例数量。实际原始路径分别见 TEST_MATRIX.json 和 EVIDENCE_MANIFEST.json；本报告不回写旧结果为成功。

source8b/F8的scripted r1已只读核对：core5/18、multi1/10、professional3/8、adversarial2/4均FAIL；文件Runtime为scripted30/30、资格false。四套隔离配置误用Mock导致SemanticPlanner消耗第一条脚本响应，不能把completed解读为PASS，也不能将此配置不兼容直接当成产品业务回归。原始报告保留在build/v1600-evidence/20261002-source8b-current-evals-r1。没有独立网络/付费计数器，逻辑completion次数不是实际付费次数，不补填0。fresh r2已使用既有空凭据默认profile合同在新隔离目录完成；当前129d/B719源码core18/18、multi10/10、professional8/8、adversarial4/4为40/40，文件Runtime30/30。五个报告路径/hash见build/v1600-evidence/20261002-source129d-current-evals-r2/envelope.json；fresh_r2_created=true，不改变桌面默认配置。四套逻辑completion合计123，不能称为123次真实网络/付费；独立计数仍null/NOT_RECORDED。旧r1失败未被重写。

## 3. 新发现P1费用问题与修复

最小真实调用发现：未配置价格时，旧后端费用估值和SQLite兼容列为0，前端也显示0，美元预算可能永远不耗尽。实际usage确认为20Token，但价格未知，费用应是未知而不是免费。

已实现主要链路：

- 冻结Provider/端点hash/模型价格快照；拒绝缺项、布尔、负数、NaN、Infinity，不自动写官方价格。
- DTO统一 cost_status、nullable estimated_cost_usd、known_cost_usd、unknown_cost_requests、pending_cost_requests。
- 根任务租约下BEGIN IMMEDIATE原子预算预留；model_run写入与结算同一事务。
- 正美元预算禁用隐含重试；所有Planner/子Agent/压缩/Verifier/修复阶段共享根任务，未知结算停止后续工具调用。
- 自动标题使用本地fallback，避免预算外隐含付费。
- 前端费用未知不填零；真实零费率仍可确认零费用。

最终复核发现的取消后持久化失败/日志遮错/兄弟继续漏洞已修复：成本安全异常保持优先，同数据库/根任务/generation进程熔断作为写库失败后备。2个故障反例先RED再GREEN，最终218项组合通过；冻结后的当前CLEAN源码r8全量门禁已通过，历史定向数量不与全量数量相加。

2026-10-02T02:33:29Z已实际执行一次独立成本smoke：新合成DB和专属输出、固定公开短句、无工具/工作区/用户会话，output cap16、retry0、预算USD0.01。官方DeepSeek请求精确绑定requested_model deepseek-v4-flash及endpoint hash；只证实requested_model，不假定服务端实际模型身份。生产reserve、reservation_observed、原子settle、check_result各1，独立check_result与usage_summary一致，1条model_runs、完整usage/price_snapshot、known估值USD0.00003432、unknown0/pending0。两次最小调用合计40tokens；旧调用unknown price/兼容存储0保持原样，新显式输入1.32/输出3.96 USD每百万仅为在用户授权下采用的显式保守测试估值，合计估值/实际账单仍未知。HTTP transport boundary确计1，socket及服务商billed request未记录；HTTPX timeout30是分阶段超时而非总wall-clock上界。worker离线self-check-r2通过；source前后及旧证据size/mtime/hash均未变。报告为build/v1600-evidence/postfix-cost-paid-smoke-v16-r1.json，不收录DB或凭据。

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
2. 路径测试最小修复、双布局定向和新checkpoint已完成；fresh完整质量r8 PASS，当前fresh r2四套40/40和文件Runtime30/30完成；不修改合同换取PASS，r7及旧r1失败保留。
3. rc10构建引导失败已独立留存candidate-build-source129d-rc10-envelope.json，PS5 native quote strip导致ExistingCandidateDependencies的Python -c SyntaxError；未创建候选或开始编译，根portable与源码未变。当前独立ignored pwsh7 adapter构建rc11已实际exit0，目录build/candidates/v16.0.0-candidate-20261002-rc11，build_id c7ab3ce53a888c5dfc772d08；candidate-build.json hash e614f04c9f1a39cfc2d9a64bafc77a39f5007ae1d5a86f03da581f4096cf0823，最终envelope为build/v1600-evidence/candidate-build-source129d-rc11-envelope.json。源码前后129d/B719 CLEAN、根portable三hash未变；三份portable artifact bytes/hash已核对。原始build-only receipt保持BUILT_NOT_RUNTIME_ACCEPTED。后续实际portable冻结runtime 8执行全部exit0且无timeout，envelope为build/v1600-evidence/candidate-runtime-rc11-r1/envelope.json、PASS_RUNTIME_ONLY；Sidecar artifact reopen/render、Windows中文TTS/nativeVAD依赖探针通过（stt_models_loaded0），6桌面三组件nonce/PID/mainHWND与身份原始receipt匹配且主窗关闭/owned descendants0。5计时中位3002ms/最差3162ms，非配对基线或人工验收。当前NSIS/MSI bundler实际exit0，candidate-installers.json raw状态BUILT、installedfalse/no_download_policy_verified=false/signedfalse保持不变；webview_install_mode skip是配置，不替代产物独立验证。两包位于build/candidates/v16.0.0-candidate-20261002-rc11/installers，NSIS68274134B、SHA25634e6049b037a3a6eb81c7eeb001c718c8562454a9e167fbb655f7da7035f10e2；MSI93859263B、SHA2568eed3ec628c5f0872f00f70236030becb3785c30bad2ea30aa7b452094b91680。范围BUILT_NOT_INSTALLED_NOT_RELEASED，未真实安装/升级/卸载或覆盖根portable；主代理已确认构建临时.gitkeep恢复后129d/B719 CLEAN，本次仅引用既有确认，不重新capture。19自动门禁+1构建共20/42技术门禁，其余22保持BLOCKED，不能称RC就绪。
4. 安装原型只读审核发现7项问题，build/v1600-evidence/read-only-installed-lifecycle-rc10-audit-v16-r1.json为BLOCKED_FOR_EXECUTION；新安全adapter仅准备中。官方8.0.1 NSIS/MSI已digest核验且只读MSI UpgradeCode匹配，download-receipt见build/upgrade-baseline/v8.0.1-official-20261002，但未安装/执行。精确归属/碰撞检查、合成fixture、备份与恢复合同就绪后再真实安装/升级/卸载；实际主窗和nonce三组件收据不能用弱CloseMainWindow替代。
5. 已选已安装qwen3:4b、目标16384窗口；此前r3和最新owned-qwen r1均为localLLM admission BLOCKED（第7节），继续只读观察，不下载/改默认Provider/降低门槛/关闭用户应用。独立当前small CPU/int8固定公开TTS音频合成转写已DEVELOPMENT_PASS，报告build/v1600-evidence/synthetic-stt-rc11.json：归一化transcript一致/similarity1.0/5251ms，运行前RAM5186543616B通过3GiB测试余量，原2GiB保护不变；4既有模型文件复制至隔离runtime，无download，原4hash前后相同，ownedJob active0/ownedprocessstoppedtrue。仍未使用实际麦克风，UI cancel/retry和设备闭环未验收；不并行重负载，不授予localqwen/fileAgent模型资格，也不提升20/42门禁。
6. 最小真实production成本链路已经通过；默认云模型完整Agent合同仍需固定有限预算单独验收，最小HTTP成功不授予文件Agent资格。
7. 补实际界面工作流和可比基线；预tag门禁就绪后再上传/tag/下载复核及完整备份便携替换。

当前尚无本版本正式tag、远端发行、真实用户库迁移或根portable替换。无需重写Agent内核、Executor、权限、Memory或现有文件面板；继续填真实证据，不扩大产品范围。

独立参考资料已下载：../参考资料/deepseek-harness，官方deepseek-ai/deepseek-harness，commit639ed015397290b3745d163aafe02ffee4aa3f84。仅供只读参考，未安装/启动/调用模型，不影响或更改司忆Agent架构。

最新三份ignored证据仅做静态/离线补充（路径/hash见EVIDENCE_MANIFEST.json）：rc11-static-no-webview-bootstrap-v16-r1.json中，NSIS下载/最低版本更新分支编译条件均false，MSI mode0表未观察到WebView bootstrap；并非包反汇编或完整运行网络证明，标准WixUI DLL字节码未分析，LaunchApplication不是无网络证明，旧candidate-installers.json no_download_policy_verified=false保留。rc11-msi-unicode-codepoints-v16-r1.json在COM文本序列化前取得UTF-16 units并以ASCII数字传出：[21496,24518,46,101,120,101]与司忆.exe及生成WXS一致，早前肉眼乱码只属于显示链路，不报告MSI损坏，具体编码根因未验证。offline-installed-lifecycle-rc10-r2-tests-v16-r1.json为39/39 offline mocks、0失败/错误；--execute硬拒绝且production collector未接线，不授予实际安装/升级/卸载或RC，20/42不变。

## 7. 本轮 owned-qwen r1 真实预检：BLOCKED

2026-10-02T03:20:18.453290Z 已在 fresh namespace 实际执行一次 Job-owned `--execute` 预检并退出2。RAM4983595008B已达到3GiB（3221225472B），freeVRAM5264900096B低于5GiB（5368709120B），不是RAM不足；初始资源门槛即拒绝。`actual_preflight_run=true`，但raw `actual_run=false`：无selected模型克隆、服务启动、prewarm、模型加载或production Runtime CLI，planned24 local_live+6 scripted_controls均未执行。source前后均129d/B719 CLEAN，Job退出/清理active均0，rawlogs与失败收据保留。初始门槛在模型依赖/默认config/portable hash比较前结束，不能由本收据声称全用户数据或模型store已核验；未读取用户DB内容。physical network/paid计数仍null/UNKNOWN，不虚构0或费用。localBasic/fileAgent/默认模型仍未PASS，正式20/42及RCfalse、release BLOCKED不变；既有deepseek-harness只作reference-only。

- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/receipt.json`：1788B，SHA256 `3492133d31da407f628e4c1b05c3ff33d0ee15aba97aa0e2ea01e7416113eb86`。
- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/job-envelope.json`：745B，SHA256 `7d93cdc3288ce34f1f1c62e9fef53c2ea0ba6353fb710be0eeae07e3982e9dce`。
- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/worker-raw.log`：351B，SHA256 `1c36cfc163bb16a7403051d9e0fc352cd2c7f859cd63d0c3714aca28ae3db2b4`。
- `build/v1600-evidence/owned-qwen-runtime-live-v16-r1.py`：39066B，SHA256 `993d88325f9da5ae148cb8640f5c6ba8cba89b42d320c4f407276558fa0c3bb1`。
