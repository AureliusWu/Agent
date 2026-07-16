# 司忆 3.0.0 运行时需求矩阵

本版本依据 `CC_SOURCE_REUSABLE_PARTS_FOR_AGENT (1).md` 的行为规格进行 clean-room 实现。`claude-code-main` 仅用于理解交互与状态机，不复制其源码、注释、提示词或产品代码，也不纳入发布制品。

## 3.0 核心范围

| 能力 | 实现 | 自动验证 |
| --- | --- | --- |
| SQLite 持久对话队列 | `queue_service.py`、Schema 23 | FIFO、优先级、重启恢复 |
| 运行中继续提交 | `/api/tasks` + Composer“排队” | 同对话串行执行 |
| Query Guard | conversation lock + `ActiveRunControl` | `idle/dispatching/running` 状态 |
| 安全点引导 | `/tasks/{id}/steer` + safe-point consume | 真实两轮模型调用 |
| 分层取消 | `cancellation.py` | 父子传播、注册表释放 |
| 工具中断策略 | `ToolSpec.interruptibility` | `cancel/block` 元数据 |
| 真正停止链 | Task 取消 + Windows 进程树终止 | 任务与命令回归测试 |
| 结构化工具回执 | `ToolReceipt` | 退出码、变更、截断信息 |
| 大输出制品 | `artifact_store.py` | offset/limit 增量读取 |
| 读取缓存与无进展检测 | `TaskReadCache` + round fingerprint | 缓存失效与终止回归 |

## 操作语义

- **Submit**：当前运行中时创建独立任务并进入队尾。
- **Steer**：只作用于当前运行任务，在模型或工具批次结束后的安全点注入。
- **Promote**：提升尚未执行队列项的优先级。
- **Cancel queued**：取消指定排队项及其对应未执行任务。
- **Stop**：终止当前模型流、子执行链和可取消的本地进程树；已完成副作用保留审计记录。

## 延后范围

LSP、Hook 插件生命周期、完整后台任务类型、Worktree 隔离及长期 MCP 连接管理不属于本次 3.0 核心升级。已有能力保持不回退，后续按独立版本迭代。
