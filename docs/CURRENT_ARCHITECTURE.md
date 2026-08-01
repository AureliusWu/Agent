# Current Architecture Baseline

## Runtime

司忆 `11.0.1` 以 Tauri 2 Windows 桌面壳启动 FastAPI sidecar。React 前端通过每次进程启动生成的本机令牌访问对话、任务、文件、Skill、MCP、记忆、产物与设置。SQLite Schema v36 保存持久任务、执行分段、队列、检查点、工具与 Skill 轨迹，以及产物文件名和内容指纹。v11.0.1 恢复夏目心正式角色资产，并将搜索链路扩展到用户全局记忆、当前项目记忆和个人长期记忆，项目记忆仍严格限定在当前显式选择的工作区。

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
