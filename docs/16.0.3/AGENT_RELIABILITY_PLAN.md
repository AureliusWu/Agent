# 司忆 v16.0.3：Agent 可靠性升级

对照基线：司忆 v16.0.2，`a64333d`。研究日期：2026-11-06。沿用 Python/FastAPI、SQLite、Tauri 和现有权限内核；不安装上游、添加依赖或迁移数据库。

| 上游公开资料 | 可借鉴机制 | 司忆的决定 |
| --- | --- | --- |
| [Claude Code：项目指令与路径规则](https://code.claude.com/docs/en/memory#path-specific-rules) | 项目指令应与当前涉及的路径匹配 | 用任务声明的目标路径确定作用域，不把兄弟目录的规则当成全局。动态 glob、个人记忆导入暂不做。 |
| [Codex：AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) | 每层至多一个有效文件，override 优先，按根到叶合并并限制总大小 | 司忆不再注入同目录两份相互冲突的规则；约束在 32 KiB 的实际提示词内。明确引用的 README 只是资料，仍标记工作区输入的安全属性，不增加权限。 |
| [DeepSeek Harness：会话检查点](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session/session-checkpoint-policy/README.md) | 跨模型／工具边界先确保持久状态；崩溃后未结算调用不能当作未执行 | 复用现有任务检查点，让无工作区对话也在安全点原子接收引导，旧提议不得抢跑。不是全量请求快照或工具 exactly-once。 |
| [SuperAgent 原型：停止与重新授权](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/docs/002-authorization.md)、[稳定入口身份](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/src/core/intake.ts) | 人工停止后不能自动续跑；稳定身份用于避免重复派发 | 终态恢复入队前拒绝，执行端再检查；使用已有任务 ID 读回丢失回执，仅同会话才接续显示。公开研究原型不是生产权限模板，不复制其宽权限。 |

## 验收次序

1. 复现指令重复和作用域泄漏、旧引导丢失与旧工具抢跑、终态回退、丢失响应后的重复操作风险。
2. 在隔离 SQLite／合成工作区下回归：覆盖空 override、超限和链接防护，检查模型前持久化与事务回滚，检查取消后的队列／事件／模型调用数。
3. 验证前端对同任务同会话的单次 GET 回查；明确拒绝不回查，无证据不假装已提交，SSE 事件不重复拼接，保留用户原始消息。
4. 按冻结源码执行构建、回归和无副作用制品冒烟；另用 DeepSeek 合成输入做一次有输出上限的真实模型探针，不执行工具。它不替代真实模型任务集、安装、签名或发布验收。

已有任务原子入口、每轮工具准入、人工待办、集中权限、执行收据和独立验证继续使用。后续候选：按实际访问路径延迟装载规则及来源可观测性；覆盖多执行器的完整检查点与交互式副作用对照。暂不接入 GitHub／飞书自主执行、遥测或新增常驻服务。
