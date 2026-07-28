# v9.5.0 上下文与恢复数据可靠性审计

结论：`PASS`。

- 压缩前 checkpoint 保存用户目标、约束、已完成、待完成和 typed working memory。
- FACT、INFERENCE、CONSTRAINT、DECISION、OPEN_QUESTION、NEXT_ACTION 与 EVIDENCE_REF 保持独立类型，不把推断提升为事实。
- 队列按优先级、创建时间和 ID 稳定排序，claim/consume 持久化且过期 claim 可恢复。
- 每次工具调用完成后再推进持久游标，副作用继续使用 operation execution id 去重。
- 无工作区检查点使用确定性 `no_workspace` 证据，不伪造文件或 Git 状态。
- 统一运行状态返回数据库中的真实进度、Token、队列和 checkpoint；缺失上下文明确标注 unavailable。
