# 司忆 4.0.0 能力矩阵

## 范围与排序

4.0.0 以 `CC_SOURCE_REUSABLE_PARTS_FOR_AGENT (1).md` 的 P1/P2 能力为边界，按“执行可追溯 → 上下文效率 → 源码导航 → 生命周期 → 隔离执行 → 恢复与诊断”排序。参考文档明确不建议迁移的 33–37 不在本版范围。

| 顺序 | 能力 | 4.0.0 状态 | 落点 |
|---:|---|---|---|
| 1 | 11. 结构化工具契约 | 已有，验证 | `tool_registry.py`, `tool_receipts.py` |
| 2 | 12. 大输出落盘 | 已有，验证 | `artifact_store.py` |
| 3 | 13. 工具结果预算 | 已有，验证 | `efficiency.py`, `TaskLimits` |
| 4 | 14. 自动上下文压缩 | 已有，增加 Hook | `context_budget.py`, `task_runner.py` |
| 5 | 15. 文件读取缓存 | 已有，验证 | `TaskReadCache` |
| 6 | 16. Read/Grep 折叠与无进展检测 | 已有，验证 | `compact_tool_result`, 轮次指纹 |
| 7 | 17. 第一等文件工具 | 已有，验证 | `sandbox.py` |
| 8 | 18. 命令安全策略 | 已有，验证 | 无 shell 执行、快照、进程树取消 |
| 9 | 19. 统一权限上下文 | 已有，验证 | `permissions.py`, capability token |
| 10 | 20. LSP 源码导航 | **本轮新增** | 真实 JSON-RPC 握手；Python/TS/JS/Rust；索引降级 |
| 11 | 21. 延迟工具发现 | 已有，扩展 | `select_model_tools` 按任务选择 LSP/Worktree |
| 12 | 22. Diff 与文件变更跟踪 | 已有，验证 | 可撤销备份、变更清单、快照 |
| 13 | 23. Hook 生命周期 | **本轮新增** | pre/post tool、complete、compact；异常隔离与审计 |
| 14 | 24. 后台任务系统 | 已有，验证 | 持久队列、优先级、暂停/继续/取消 |
| 15 | 25. 子 Agent 分工 | 已有，验证 | 规划/验证/并行探索 |
| 16 | 26. 子 Agent 消息作用域 | 已有，验证 | 父任务、工作区和工具白名单 |
| 17 | 27. MCP 连接生命周期 | **本轮增强** | TTL 会话复用、配置指纹、并发锁、过期与重连 |
| 18 | 28. Worktree 隔离 | **本轮新增** | `.agent/worktrees` 受管路径、全局锁、危险确认 |
| 19 | 29. 会话历史与恢复 | 已有，验证 | SQLite 消息、checkpoint、重启恢复 |
| 20 | 30. 长期记忆扫描与老化 | 已有，验证 | 候选、合并、低价值归档 |
| 21 | 31. Token/上下文/成本追踪 | 已有，验证 | 自适应预算、阶段用量、剩余量与成本 |
| 22 | 32. 环境诊断 | 已有，增强 | 统一诊断增加 Hook/LSP/MCP/Worktree 状态 |

## 自动验收边界

- 新增工具全部经过现有 ToolSpec、Agent Profile、PermissionMode、审计与取消链。
- Worktree 不允许逃逸受管目录，移除操作即使在“完全访问权限”下也需确认。
- LSP 不可用时返回明确降级来源，不伪造语言服务器结果。
- Hook 为观察与扩展点，单个 Hook 失败不能中断核心任务。
