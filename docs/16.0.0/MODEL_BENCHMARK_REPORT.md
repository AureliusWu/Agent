# 司忆 v16.0.0 模型资格与基准报告

真实文件Agent模型资格仍未确认。用户授权已经收到，不再等待模型选择或重复付费/管理员授权。

## 实际模型结果

- 已选已安装本地模型 qwen3:4b，拟用16384上下文；未下载或改默认Provider。
- GlobalMemoryStatusEx资源样本：可用RAM1147224064B、空闲VRAM4582277120B；低于本轮保守加载余量，未启动或加载Ollama。应用2GiB门槛不改，不关闭用户应用。
- 一次DeepSeek固定公开短句最小HTTP验证成功：1次、0重试、17输入+3输出=20Token、722ms；实际用量从隔离SQLite只读补充核实，无额外请求。
- 该调用由外部保守估值、16输出Token、一次调用限额约束；未配置生产单价，旧存储0必须解释为费用未知，不是免费或账单证明。
- 发现P1成本预算问题后已实施统一未知语义/端点定价/原子预留/错误传播与本地标题fallback；最终取消持久化错误链、同代熔断等218项定向回归通过，待新CLEAN全量证明，不继承旧候选证据。
- Basic/只读工具/结构化计划/文件Agent四级资格仍false。最小HTTP不能当真实Agent Eval或本地模型资格。

## 当前源码与历史区分

费用和中文路径修复后必须重新冻结源码、跑当前合同、重建CLEAN候选。旧rc9对应447A指纹与ac4cb78e8db86ffe561519cb build_id，DIRTY；不是当前修复后的证据。

旧原始scripted结果保持历史：core18/18、multi_agent10/10、professional8/8、adversarial4/4；文件Runtime10场景×3为30/30。真实隔离NTFS/SQLite/Executor/权限执行，模型响应脚本化，没有实际LLM资格。

旧core tool_error_rate0.0811和adversarial0.25含预期拒绝，不改成0。无同合同/模式/来源/环境基线，NO_REGRESSION仍NOT_VERIFIED；缺baseline不能自动PASS。

## 可复用真实入口

app.evals.local_model_benchmark.runtime_file_cli --mode local_live：3次/场景，真实本地模型场景8×3，加2×3 scripted安全控制。不能将30条都称为真实推理。必须验证模型digest、有效/api/ps窗口、端点/config身份，漂移则资格无效。

模型资格绑定Provider、端点hash、配置hash、model digest、显式限制与源码；TTL/配置/窗口未知不继承。保持小输出预算，不用抬高Token预算掩盖能力问题。

## 语音边界

small是独立STT模型，不是qwen3文件Agent资格。冻结faster-whisper1.2.1、CTranslate2、PCM/VAD及Windows中文TTS已验证；small固定音频语义转写/真实麦克风尚未通过。当前资源低于原应用2GiB及本轮3GiB转写余量。

后续自动继续：完成费用最终修复与回归 → 当前源码scripted合同 → 新CLEAN冻结包 → 资源足够时真实本地LLM和麦克风 → 固定预算云模型Agent合同。保持原默认配置及用户数据，不用模拟绿灯替代真实证据。
