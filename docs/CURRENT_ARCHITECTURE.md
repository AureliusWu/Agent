# Current Architecture Baseline

## Runtime

司忆 `14.0.0` 当前运行基线以 Tauri 2 Windows 桌面壳启动 FastAPI sidecar。React 前端通过每次进程启动生成的本机令牌访问对话、任务、文件、Skill、MCP、记忆、产物、设置与本地语音链路。SQLite Schema v42 保存显式任务状态、需求与验收条件、依赖图、预算、专业角色消息、性能轨迹、Provider 策略、Tool Receipt、文件事务、回滚记录及脱敏后的语音/STT 状态；录音不持久化，STT 不额外保存转写原文副本，但用户确认或符合自动发送条件后提交的转写文本属于普通用户消息，按现有会话规则持久化；TTS 幂等明文不持久化。麦克风原始音频不进入 Ollama 或任何模型 Provider；仅已发送的转写文本按普通消息路由，且仅当当前 Provider 选为 Ollama 时才由 Ollama 接收这条文本。新安装默认使用 Faster-Whisper `small` 的 CPU `int8`，模型仍须用户确认后下载，已有模型选择不会被升级覆盖。最终完成状态只能由独立验证器提交。受控多 Agent 保持子角色只读、根 Executor 唯一写入，专业 Agent 使用最小工具范围，扩展角色提示按不可信数据封装。文件变更继续使用版本令牌、原子写入、可恢复删除和批量预检回滚；Windows 停止链路会终止子进程树后再确认取消状态。

## Agent Core

`runtime/runner.py` 组合语义 Planner、模型路由、受控工具、独立 Verifier 与限定 Repair。Token、轮数、工具次数和单段超时默认只触发 ExecutionSegment 检查点、结构化压缩与续跑；显式费用上限、安全阻断、连续无进展或不可恢复故障才停止。ToolScheduler 依据并发元数据调度，所有工具继续经过 ToolSpec、权限、工作区沙箱、审计、快照与取消链。

## Identity And Memory

固定 `agent_id=natsume-kokoro-001` 的身份内核不依赖工作区或 Provider。长期记忆区分语义、情景、程序和关系记忆，记录来源、置信度、敏感性、确认、锁定、替代和墓碑。Context Assembler 按预算装载身份、情绪关系、相关记忆、基础Agent约束和当前任务。

## Capability Truth

`GET /api/capabilities/runtime` 是界面解释当前能力的统一入口。无工作区聊天与安全公网搜索可用；文件和命令能力只在用户主动选择工作区后可用；远程 MCP 只有在启用且真实工具发现成功后才可用。Tavily/Brave 未配置或健康检查失败时不进入模型工具列表。

## Desktop And Release

桌面端动态选择 sidecar 端口、等待健康检查、限制异常重启并在退出时清理进程。API Key 存入 Windows Credential Manager。正式构建自动注入 Git、DIRTY/CLEAN、源码指纹、构建时间、Tauri/React/Sidecar Build ID 和 Schema；NSIS、MSI、SBOM、覆盖升级、卸载数据保留与 Sidecar 清理纳入发布脚本。

## Verification

后端 pytest 覆盖率门槛为 70%，另有前端 lint/build/security、Rust、核心/安全/多 Agent/专业 Agent Eval、真实 DeepSeek 身份与工作区验收。180 项定向 manifest 逐项要求可追溯证据；缺少独立场景证据时保持 `blocked`，不得由汇总测试自动判为通过。

## Deferred Scope

主仓库仅保留 Windows PC 端，网页/PWA、移动端及非 PC 部署实现归档在相邻的 `../Agent非PC端`。云同步、多人协作、最终视觉重构和角色动画仍未实施；插件市场、第三方任意代码、任意深度子 Agent 和自动放宽权限仍不开放。
