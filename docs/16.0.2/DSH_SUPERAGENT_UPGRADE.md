# 司忆 v16.0.2 开发计划：可靠的本地任务交接

目标：用户发送任务或在模型运行中补充指令时，司忆不会因进程中断而留下半条任务，也不会在已收到引导后启动尚未执行的旧工具批次。此文档是下一版本开发记录，不是发行或升级验收证明。

## 研究来源与现状

基线：司忆 `main` 的 `9661b65`（16.0.1），从远端快进后建立本地开发分支。上游以 Git 提交锁定；只审阅源码及文档，未安装或启动上游程序。

| 参考 | 已核实的做法 | 司忆现状与决策 |
| --- | --- | --- |
| [DSH Agent Loop](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/core/agent-loop/src/agent.ts) / [检查点策略](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session/session-checkpoint-policy/src/index.ts)（`0.2.2-rc.1`） | 持久会话事件与临时流分离；在模型/工具边界提交会话前缀 | 司忆已有 SQLite 事件、工具收据、租约和断点续跑。工作区引导以前先标记已消费，再附加到模型内存；模型给出工具时也没有在该边界复查引导。复用现有检查点和队列事务。 |
| [DSH SuperAgent prototype](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/8c32a9dc5d72c033816c6a8110b37f40cf94fba4) / [入口去重](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/core/intake.ts) / [授权](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/002-authorization.md) | 稳定 Trigger ID、先做路由和授权，再进入 Ticket；公开仓库是研究原型，不等于其内部版本或论文全部设计 | 司忆已有本机稳定 task_id、权限和工作区围栏，但任务、消息和队列分事务写入。先保证原子提交；相同 ID 保持 409，不接受第二次执行，也不重新绑定凭据。 |

## 本轮实施及验收

| 要求 ID | 目标 | 验收 |
| --- | --- | --- |
| SIYI-1602-INTAKE | 用户消息、任务、初始事件和持久队列共同提交；写入失败不留半任务；已提交的工作不因唤醒失败被删除 | 新隔离库注入队列写失败、重复 ID、提交后唤醒失败；比对数据库状态和任务 API |
| SIYI-1602-STEERING | 工作区执行器在下一个模型/工具安全点确认引导；确认和检查点一起提交；新引导取代未启动的旧工具提议，旧检查点不能绕过已生效引导 | Mock 模型延迟期间提交引导；第二次模型调用前核对检查点、队列和调用参数；无旧文件、无旧工具收据；注入已取消项验证事务回滚；验证旧检查点拒绝、最新检查点恢复 |
| SIYI-1602-COMPAT | 无数据库迁移、无新依赖、无新权限；不改变恢复中已启动的工具、跨会话串行和独立验证 | 运行时/API 测试，固定核心和专项脚本 Eval，版本元数据及隐私检查 |

执行顺序：先固定对照源码，再分别复现半任务和过期工具批次，完成最小修复，最后执行隔离回归和对照测量。新增引导检查点含条目 ID；事件只增加检查点序号，不增加凭据或引导正文。任何权限校验、路径隔离、成本、截止时间、取消、租约与验证规则继续由原链路决定。

保留已有启动回填逻辑，用于旧数据库缺队列行的兼容；它只能从任务记录重建精简参数。新入口在确认提交前保存原始请求的完整队列参数，不再依赖这种降级。无数据库 schema 变更，也不新增凭据存储位置。

## 不在本轮接入

- DSH 按模型轮次冻结已公布的工具集合值得独立验收。司忆已有动态工具发现；不能把较宽的发现目录当作执行许可，也不能破坏旧检查点恢复。另测同轮发现、并发预取、旧批次恢复后再合入。
- SuperAgent 的 [人工关注面板](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/005-panel.md) 可在既有可恢复任务和 SSE 上做只读聚合；不新建任务系统。需先定义去重、可见范围和输出字段。
- 无工作区聊天仍沿用原引导路径；本轮的引导检查点保证仅适用于工作区执行器。引导不会重写任务合同或撤销已经发起的副作用；要立刻停止任务仍应使用取消操作。
- 不引入 Cordis/Node 后端、GitHub/飞书入口、后台调度、远端写入、Telemetry 或新的自动付费调用。上游原型的部署说明不是司忆的运行指令。

## 交付边界

下一版本改动在本地开发分支维护。现有 `VERSION`、16.0.1 制品和正式 RC 证据仍保留；进入 v16.0.2 候选时再统一更新版本源、打包和采集新一轮同提交证据，不沿用旧通过报告。未经独立验收不替换用户程序、数据库、模型或密钥，不对外标记正式发布。

## 本地验证记录（2026-11-06）

- 在 `9661b65` 的独立源码快照中运行新回归，观察到旧行为：写入队列失败后还留有 1 条用户消息；提交后唤醒失败返回 503；引导到达后旧提议仍发生 1 次工具调用。保留这些预期失败，不改变断言。
- 当前代码定向回归 67 项通过，包括 SQLite 错误回滚、语音会话绑定、重启唤醒、过期提议丢弃、旧检查点拒绝和从最新检查点续跑。API 状态与独立 SQLite 读取互证；数据可靠性判断仅覆盖这些隔离场景，不使用用户库。
- 扩展回归覆盖 runtime、voice、security、plugins、evals：755 通过、1 跳过（需显式选择真实 Ollama）。扩展采集在最终的 worker 竞态分支补丁前启动，该补丁在上述 67 项最终定向回归中单独覆盖。两轮都不是全库覆盖率或正式 RC 证明。
- 固定脚本 Eval：核心 18/18、多 Agent 10/10、专业 Agent 8/8；没有误判完成或权限/沙箱违规。使用 `scripts/eval.ps1 -Mode scripted_runtime -RequirePassed`，对应 suite 为 `core`、`multi_agent`、`professional_agents`。首次专业集使用错误的 suite 名，纠正为 `professional_agents` 后成功，未修改集合。
- 前端 lint、安全/桌面/版本身份检查、TypeScript + Vite 生产构建均通过；Vite 仍提示主 JS 块超过 500 kB。Rust `cargo test --locked --offline` 17 项通过。现有 16.0.1 的 16 个版本源一致。
- 同机入队对照：两侧各 5 个独立进程/数据库，每个 3 次预热、12 次计时，交错执行；p50 **53.91 → 24.30 ms**，p95 **59.09 → 27.61 ms**。两侧 `quick_check` 通过、任务/消息/事件/队列数量一致、模型调用为 0。只测 Python + SQLite 入队，不含 HTTP、模型或界面耗时；样本有限，后台有并行测试负载。原始报告在被 Git 忽略的 `build/v1602-intake-bench-f972b022/comparison-final/measurements.json`。
