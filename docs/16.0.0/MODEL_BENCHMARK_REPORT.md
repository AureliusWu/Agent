> PUBLICATION NOTICE / 公开脱敏展示说明：本文件来自历史 v16 候选提交 `129d16b87fcdea42c319a693ece438d9bf09ae9b` 对应的未提交证据备份，仅机械替换本机绝对 Windows 路径；状态、FAIL、门禁和数值未被升级。本轮源代码合并不等于重新验收或正式发布。原始文件字节保存在私有本地备份，原始 SHA-256 为 `66EA282D7B977FAF3DED5C55DF287C0606C733B345978054986116C5B7461B9D`。正文中的原制品或原文档 SHA 仅适用于私有原件，不能用于验证本公开展示副本。
# 司忆 v16.0.0 模型资格与基准报告

真实文件Agent模型资格仍未确认。用户授权已经收到，不再等待模型选择或重复付费/管理员授权。

## 实际模型结果

- 已选已安装本地模型 qwen3:4b，拟用16384上下文；未下载或改默认Provider。
- 旧GlobalMemoryStatusEx样本可用RAM1147224064B、VRAM4582277120B不足，原始记录保留。2026-10-02T02:11:46Z只读刷新：可用RAM6108884992B、VRAM5676990464B，达到保守GPU独立检查采样门槛；尚未启动或加载Ollama。该r2仅为历史ready样本；历史r3 2026-10-02T02:41:48.250197Z：RAM4275191808B、VRAM5235539968B，GPU<5GiB且CPU/unknown-offload RAM<7GiB，localLLM admission BLOCKED，source前后129d/B719 CLEAN但source qualification false，未加载/录音。资源采样不是模型/源码资格，负载前重新观察；应用2GiB门槛不改，不关闭用户应用。
- 历史一次DeepSeek固定公开短句最小HTTP验证成功：1次、0重试、17输入+3输出=20Token、722ms；实际用量从隔离SQLite只读补充核实，无额外请求。
- 该调用由外部保守估值、16输出Token、一次调用限额约束；未配置生产单价，旧存储0必须解释为费用未知，不是免费或账单证明。
- 发现P1成本预算问题后已实施统一未知语义/端点定价/原子预留/错误传播与本地标题fallback；最终取消持久化错误链、同代熔断等218项定向回归通过，当前129d/B719 CLEAN管理员r8全量已证明；不继承旧候选证据。
- 2026-10-02T02:33:29Z第二次最小公开短句实际付费成本smoke PASS：1次HTTP transport、retry0、output cap16、预算USD0.01，17输入+3输出=20tokens；production根task/lease/reserve→model_run与settle同事务→check_result链路通过，1条model_runs、完整usage/price_snapshot，pending0、unknown0、known估值USD0.00003432，独立check_result与cost_totals一致。报告build/v1600-evidence/postfix-cost-paid-smoke-v16-r1.json；source与旧证据未变。
- 新price_snapshot精确绑定requested_model deepseek-v4-flash/endpoint hash；served_model未独立记录。输入1.32、输出3.96 USD每百万仅为在用户授权下采用的显式保守测试估值，不是官方费率或账单硬上界。两次最小调用共40tokens；旧unknown价不回填，新known估值不充当两次总费用，实际账单未知。physical_socket/provider_billed计数null；HTTPX timeout30为分阶段而非总deadline，无工具/工作区/用户会话或默认配置修改。
- Basic/只读工具/结构化计划/文件Agent四级资格仍false。最小HTTP不能当真实Agent Eval或本地模型资格。

## 当前源码与历史区分

当前CLEAN checkpoint129d16b/B719已完成（路径测试合同修复）；完整管理员质量r8最终PASS：1990PASS/21SKIP/0FAIL，覆盖率83.73579017757635%，Rust17和前端lint/build/security/desktop全PASS，19项自动门禁true。proof/envelope分别为build/v1600-evidence/full-backend-20261002-v16-r8-admin.json与admin-full-backend-v16-r8-envelope.json。正确profile的fresh当前r2脚本合同已完成；rc10在PS5/Python-c构建引导阶段exit1失败，未创建候选或编译，失败收据保留；当前rc11候选实际构建exit0，build_id c7ab3ce53a888c5dfc772d08，最终candidate-build-source129d-rc11-envelope.json为BUILT_NOT_RUNTIME_ACCEPTED；三份portable artifact hash匹配，源码前后CLEAN且根portable未变。后续实际portable冻结runtime全部8执行exit0/no timeout，build/v1600-evidence/candidate-runtime-rc11-r1/envelope.json为PASS_RUNTIME_ONLY；Sidecar artifact reopen/render与Windows中文TTS/nativeVAD探针通过，未加载STT。6桌面warm启动原始nonce/PID/mainHWND/三组件身份匹配且主窗关闭、owned descendants0。5计时[3002,2641,3162,3094,2612]ms、中位3002/最差3162，未配对旧版、不是人工或已安装验收。NSIS/MSI bundler实际exit0，candidate-installers.json raw为BUILT、installedfalse/no_download_policy_verified=false/signedfalse；两包bytes/hash已独立核对，范围BUILT_NOT_INSTALLED_NOT_RELEASED。skip仅为WebView配置，不等于包不下载策略已验收；未安装，20/42门禁及发布BLOCKED不变。旧rc9对应447A指纹与ac4cb78e8db86ffe561519cb build_id，DIRTY；不是当前修复后的证据。

旧原始scripted结果保持历史：core18/18、multi_agent10/10、professional8/8、adversarial4/4；文件Runtime10场景×3为30/30。真实隔离NTFS/SQLite/Executor/权限执行，模型响应脚本化，没有实际LLM资格。

旧core tool_error_rate0.0811和adversarial0.25含预期拒绝，不改成0。无同合同/模式/来源/环境基线，NO_REGRESSION仍NOT_VERIFIED；缺baseline不能自动PASS。

较新但仍非当前129d的source8b/F8 scripted r1：core5/18、multi1/10、professional3/8、adversarial2/4均FAIL，文件Runtime scripted30/30、四级资格false。四套错误使用隔离Mock配置，provider_ready为true使SemanticPlanner消耗第一条脚本action；原始失败不能改写。逻辑completion请求次数不是真实网络或付费计数；独立counter未记录，明确未知而不是0。fresh r2已使用原有空凭据默认profile、新隔离输出完成，不改变桌面默认设置。当前core18/18、multi10/10、professional8/8、adversarial4/4，共40/40；文件Runtime scripted30/30，四级资格仍false。五报告路径/hash由build/v1600-evidence/20261002-source129d-current-evals-r2/envelope.json留存，fresh_r2_created=true；四套逻辑completion合计123，独立network/paid count仍null/NOT_RECORDED，文件Runtime记录0条successful model request也不等于独立网络计数0。旧source8b r1/rc9原始证据不重写。

## 可复用真实入口

app.evals.local_model_benchmark.runtime_file_cli --mode local_live：3次/场景，真实本地模型场景8×3，加2×3 scripted安全控制。不能将30条都称为真实推理。必须验证模型digest、有效/api/ps窗口、端点/config身份，漂移则资格无效。

模型资格绑定Provider、端点hash、配置hash、model digest、显式限制与源码；TTL/配置/窗口未知不继承。保持小输出预算，不用抬高Token预算掩盖能力问题。

## 语音边界

small是独立STT模型，不是qwen3文件Agent资格。冻结faster-whisper1.2.1、CTranslate2、PCM/VAD及Windows中文TTS已验证；后续独立small CPU/int8固定公开TTS音频真实冻结STT已DEVELOPMENT_PASS，但真实麦克风、UI cancel/retry和设备闭环尚未通过。历史r3 RAM样本高于原应用2GiB及本轮3GiB余量，可另行复核非LLM语音资源，不与重负载并行；未录制真实麦克风；合成冻结STT已通过，实际mic/UI操作前仍必须复核动态余量。

后续自动继续：当前全量质量/脚本合同与最小真实成本smoke已完成 → rc11 build-only已完成，portable冻结runtime已PASS_RUNTIME_ONLY，installer字节已生成，已获限定静态no-WebView证明，待完整运行网络及实际安装生命周期 → 资源足够时真实本地LLM和麦克风 → 固定预算云模型Agent完整合同。保持原默认配置及用户数据，不用模拟绿灯替代真实证据。

官方v8.0.1 NSIS/MSI已仅下载并digest核验，MSI只读UpgradeCode匹配；安装原型只读审核7项问题为BLOCKED_FOR_EXECUTION，未执行真实安装/升级/卸载或配对启动基线。独立deepseek-harness参考仓库commit639ed015397290b3745d163aafe02ffee4aa3f84仅下载，未安装/启动/模型调用，也未改变司忆Agent架构。

当前合成STT独立报告build/v1600-evidence/synthetic-stt-rc11.json：C7/16.0.0冻结backend、129d/B719 CLEAN源码前后一致；公开fixture「这是本地语音转写测试。」归一化transcript一致，similarity1.0、round_trip5251ms。small为CPU/int8，既有4文件仅复制至隔离runtime，无下载且原4hash未变；运行前RAM5186543616B通过3GiB测试余量，原生2GiB保护未改，ownedJob active0/ownedprocessstoppedtrue。microphone_used=false、real_microphone_accepted=false，不能替代本地qwen或文件Agent资格，manual voice和20/42门禁保持不变。

安装方面的新增静态no-WebView/COM Unicode一致及39/39 lifecycle offline mocks见EVIDENCE_MANIFEST.json；不是完整网络、实际安装或升级证明。旧no_download_policy_verified=false保留，--execute硬拒绝、production collector未接线，20/42及release BLOCKED不变。

## 最新 owned-qwen r1：真实预检 BLOCKED，未开始模型验收

2026-10-02T03:20:18.453290Z 一次 fresh、Job-owned `--execute` 已真实执行预检并exit2/BLOCKED；它比前述r2/r3资源样本新，旧样本与失败报告均原样保留。RAM4983595008B已过3GiB（3221225472B），freeVRAM5264900096B低于5GiB（5368709120B），故初始门槛拒绝。`actual_preflight_run=true`而raw `actual_run=false`：没有selected模型clone、服务/prewarm/模型加载/production CLI；计划24条live sample与6条scripted control未执行，不生成或搬迁Runtime报告argv。source前后129d/B719 CLEAN一致，Job退出和cleanup active均0，raw日志/收据保留。初始门槛早于选定模型、默认config和portable的hash比较，未读取用户DB，不能夸大成全用户数据/store不变核验。physical network/paid计数null/UNKNOWN，费用未记录；Basic/只读工具/结构化计划/文件Agent以及默认模型Agent资格未提升，正式20/42、RCfalse与release BLOCKED不变。deepseek-harness保持reference-only。

- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/receipt.json`：1788B，SHA256 `3492133d31da407f628e4c1b05c3ff33d0ee15aba97aa0e2ea01e7416113eb86`。
- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/job-envelope.json`：745B，SHA256 `7d93cdc3288ce34f1f1c62e9fef53c2ea0ba6353fb710be0eeae07e3982e9dce`。
- `build/v1600-evidence/accepted/owned-qwen-runtime-live-v16-r1/worker-raw.log`：351B，SHA256 `1c36cfc163bb16a7403051d9e0fc352cd2c7f859cd63d0c3714aca28ae3db2b4`。
- `build/v1600-evidence/owned-qwen-runtime-live-v16-r1.py`：39066B，SHA256 `993d88325f9da5ae148cb8640f5c6ba8cba89b42d320c4f407276558fa0c3bb1`。
