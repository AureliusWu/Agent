# 司忆 v15.0.0 候选发布说明

v15.0.0 的主题是 **Core Reliability / 可靠性内核**。本版本没有扩张 Screenshot、Clipboard、Application Control 或 Browser Agent 等高权限外延功能，而是收紧现有 Windows PC 桌面 Agent 的权限、恢复、文件、Provider、MCP、Memory 与发布边界。

## 已落地的代码能力

- 权限执行统一经过 Permission Broker 与受绑定的授权记录。`readonly` 对文件写入、命令、MCP 副作用、扩展、Skill 及管理操作实行不可通过确认解除的硬拒绝；ask、agent、full 的实际执行模式保持可审计。
- 任务恢复覆盖持久化的活动状态、租约、检查点、Provider 等待状态、任务期限、Token 与费用预算。受管进程记录可验证身份，停止和重启恢复不会仅修改数据库状态而遗留已失去所有权的进程。
- 文件沙箱补齐 Windows 符号链接、junction/reparse point、设备路径、UNC 与路径逃逸防护。`file_batch` 使用绑定会话、任务、工作区、操作摘要和过期时间的批量授权，并在执行前完成预检与冲突判断。
- 安全快照改为按声明的受影响路径创建增量操作检查点；无法生成可恢复文件副本的命令或外部 MCP 操作只记录 receipt，并在尝试恢复时以类型化错误 fail closed，不再复制整个大型工作区来制造虚假回滚保证。
- ProviderDescriptor 统一 DeepSeek、Ollama 与 OpenAI-compatible 的 Provider、端点、模型、凭据策略和能力身份。本地模型可以从已安装模型中选择，Provider 与模型状态不再依赖单一硬编码模型。
- 新增本地模型 Benchmark v1，覆盖 Basic、Tool、File Agent、Reasoning 与 Safety 场景，并记录硬件、延迟、吞吐、结构化输出、工具执行、验证与取消结果。实际模型结果必须通过版本绑定报告进入发布证据，未达门槛的模型不会被包装为通过。
- MCP v2 拆分 JSON-RPC、会话、发现、权限和 stdio/HTTP/Streamable HTTP 传输，支持持久会话、撤销、凭据绑定与类型化失败；外部工具仍受权限、网络安全、审计和操作范围约束。
- SQLite 升级至 Schema v45，并保留从 Schema v42 的备份迁移路径。新的分层 Memory 记录统一 user、workspace、conversation 与 task 所有权、来源、精确去重和审计；旧表继续作为兼容层，前端对错误所有者的数据隐藏修改入口。
- Runner 在 characterization tests 保护下拆出 orchestration、model/tool/verification loop、finalization 与 recovery policy；桌面端同步增强对话状态隔离、Sidecar endpoint epoch、进程身份校验和 Files UI 操作反馈。
- 发布流水线改为区分不可变源码提交与随后生成的证据提交，动态校验 VERSION、Python/npm/Cargo/Tauri 元数据，绑定构建指纹，并为 NSIS、MSI、升级、Sidecar smoke、SBOM 与性能对比保留可重复门禁。

## 发布状态

本文只说明当前源码中的 v15.0.0 候选能力，**不代表 v15.0.0 已 RELEASED**。正式发布仍须完成并记录：Python 全量测试与覆盖率门槛、前端 lint/build/security、Rust 测试、核心安全与恢复矩阵、本地模型 Basic 门禁、干净源码与 Git tag 一致性、Tauri/NSIS/MSI 构建、隔离安装与启动、Sidecar 健康、覆盖升级、卸载，以及 Chat、文件操作、readonly/ask、Ollama、DeepSeek、MCP、Voice 和 Memory 的人工桌面验收。

最终结论以同目录后续生成的 `RELEASE_STATUS.json`、`TEST_MATRIX.json`、`EVIDENCE_MANIFEST.json`、`IMPLEMENTATION_FEEDBACK.md`、`MODEL_BENCHMARK_REPORT.md` 与 `MODEL_BENCHMARK.json` 为准。任一 P0 门禁失败或缺少真实证据时，状态必须保持 `BLOCKED`，不得用代码存在、Mock、跳过项或开发态构建代替正式发布证据。
