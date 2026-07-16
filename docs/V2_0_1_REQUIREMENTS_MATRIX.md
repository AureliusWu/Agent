# v2.0.1 Requirements Matrix

状态：`完成`表示实现和对应验收已通过。版本号已在所有必做项和发布门禁通过后更新为 `2.0.1`。

| ID | 必做能力 | 基线 | 实施位置 | 验收证据 |
|---|---|---|---|---|
| TOK-01 | 按 Provider/模型确定上下文窗口并安全回退 | 完成 | `context_budget.py` | DeepSeek V4/未知模型测试 |
| TOK-02 | 完整请求预算、输出预留、自动分层压缩 | 完成 | `context_budget.py`, `task_runner.py` | 保护层、工具去重、阈值测试 |
| ID-01 | 唯一 `natsume-kokoro-001` 与不可变身份核心 | 完成 | `identity/`, `identity_kernel.py`, `identity.py` | 源码与 PyInstaller 单文件启动测试 |
| ID-02 | 身份版本、管理员确认与回滚 | 完成 | schema v18, identity API | 确认拒绝与版本测试 |
| ID-03 | Identity Guard 一次修复与安全降级 | 完成 | `identity_guard.py` | 漂移/技术讨论/代码引用测试 |
| MEM-01 | 统一长期记忆类型、来源与 `agent_id` | 完成 | schema v19, `long_term_memory.py` | CRUD、来源、持久化测试 |
| MEM-02 | 候选校验，模型无直接写权限 | 完成 | candidate service/API/UI | 确认前不写入测试 |
| MEM-03 | 锁定、确认、敏感、软删除 | 完成 | memory service/API/UI | 锁定/敏感/软删除测试 |
| MEM-04 | 去重、冲突、有效期、替代历史 | 完成 | `long_term_memory.py` | 新事实替代与锁定冲突测试 |
| MEM-05 | 结构化+关键词检索与可替换语义层 | 完成 | retrieval adapter | 类型、时效、来源优先测试 |
| STATE-01 | Trait/Mood/Emotion 与时间衰减 | 完成 | `affect.py`, schema/API | 衰减、持久化、事实隔离测试 |
| STATE-02 | 关系状态与事件限幅 | 完成 | `affect.py`, schema/API | 普通/重要事件限幅测试 |
| CTX-01 | 五层 Context Assembler | 完成 | `context_assembler.py` | 身份/状态/记忆/任务层测试 |
| CTX-02 | 记忆引用追踪和安全 context-debug | 完成 | schema v21/API | 引用 ID 与正文脱敏测试 |
| CONS-01 | 手动巩固、去重归档、删除墓碑 | 完成 | `memory_consolidator.py` | 不恢复删除/不覆盖锁定测试 |
| CONS-02 | 可追溯自传与连续性摘要 | 完成 | continuity schema/service | 确定性来源测试 |
| DATA-01 | 完整备份包、哈希、导入前备份与失败回滚 | 完成 | `full_backup.py`, backup API/UI | round-trip 与篡改测试 |
| AUD-01 | 身份、记忆、状态、Provider 与导入导出审计 | 完成 | audit service/routes | 去敏审计与全量测试 |
| UI-01 | 对话、记忆、状态、设置核心入口 | 完成 | React components | lint/build 通过 |
| UI-02 | 记忆来源、历史、锁定、确认、敏感管理 | 完成 | `LongTermMemoryManager` | API 联调与生产构建 |
| UI-03 | 身份版本、状态摘要和备份恢复 | 完成 | identity/state/backup panels | 生产构建与桌面验收 |
| DESK-01 | sidecar、AppData、凭据、升级数据保留 | 完成 | Tauri/scripts | sidecar schema 22/22 与桌面构建 |
| BUILD-01 | Git、时间、类型、DIRTY 与源码指纹自动注入 | 完成 | `generate_build_info.py`, Tauri/Vite/PyInstaller | 隔离 Git 仓库回归与制品健康接口 |
| BUILD-02 | Tauri、React、Sidecar 构建一致性与旧组件告警 | 完成 | `buildInfoModel.ts`, `AboutPanel.tsx` | 一致、旧 Sidecar、不完整三类测试 |
| BUILD-03 | 侧栏简略指纹、关于页完整信息和复制 | 完成 | React components | lint/build 与桌面实际启动检查 |
| CONT-01 | 无工作区身份、记忆、重启、模型切换连续性 | 完成 | identity/context/runtime | 真实 DeepSeek 18 段记录，AI 语义逐项通过 |
| WORK-01 | 无/普通/司忆源码工作区能力与权限边界 | 完成 | planner/sandbox/runtime | 真实文件、批准、恢复与审计链路通过 |

## Release Gate

当前证据：后端 `273 passed, 1 skipped`、覆盖率 `82.31%`；前端 lint/build/构建一致性测试通过；Cargo tests `4/4`；核心确定性 Eval `18/18`；Sidecar Schema `22/22`。真实 DeepSeek 链路完成无工作区多轮、新会话、完整进程重启、模型切换、清除短期上下文、普通工作区、权限批准与司忆源码工作区验收，完整记录位于 `docs/acceptance/`。本次仅在本地交付，不推送或发布；状态为“自动验收通过，等待管理员确认”。
