# 司忆 v15.0.0 实施反馈

## 当前结论

v15.0.0 的源码能力已进入候选收口阶段，但本文件当前仍是源码提交前的占位记录。自动化、安装包和人工桌面证据尚未全部绑定到最终源码提交，因此发布状态必须保持 `BLOCKED`。

## 已实现范围

- 统一 Permission Execution Contract，并让 readonly 对所有副作用 fail closed。
- 覆盖活动任务状态、租约、Provider 等待、预算、截止时间与受管进程身份的崩溃恢复。
- 加固 Windows 路径、symlink/junction/reparse point、UNC、设备路径与越界访问。
- 将命令与外部 MCP 快照改为显式 operation checkpoint，区分可恢复路径与 receipt-only 边界。
- 完成 file_batch v2 的预检、绑定授权、事务执行和回滚语义。
- 引入 ProviderDescriptor、本地已安装模型发现与选择，并新增本地模型 Benchmark v1。
- 完成 MCP JSON-RPC、会话、发现、撤销、凭据绑定与类型化失败契约。
- 将 Memory 统一到 user/workspace/conversation/task 权威记录，同时保留 Schema 42 历史升级和兼容读取。
- 在 characterization tests 保护下局部拆分 Runner 与 database.py，未重写现有 Agent/SQLite。
- 增强桌面对话隔离、Sidecar endpoint epoch、进程身份与 Files UI 操作反馈。
- 让版本、源码提交、安装包、升级基线、构建指纹与发布证据能够由同一流水线校验。

## 已知边界

- `run_command.affected_paths` 依赖调用方如实声明；空数组只产生 receipt-only 检查点。
- 外部 MCP 的工作区外副作用无法由本地文件快照回滚，只保留收据和审计。
- Runner 的 workspace-free 流程已拆出，但 workspace `_run_chat` 仍然较大；本版不做重写。
- Memory 仍保留旧表兼容层，当前是单向镜像和精确内容去重，不是语义去重。
- 小模型完整 19 项实测仅 10 项通过；Basic 3/3 通过，但 Tool、Reasoning、Safety 仍有明显能力差距。

## 证据收口规则

最终更新必须把所有 PASS 绑定到同一个源码提交，并分别记录 automated、desktop、manual 的真实证据。任何未执行、跳过、模拟替代、未绑定提交或需要用户/付费授权但未完成的门禁，都必须留在 `TEST_MATRIX.json` 与 `RELEASE_STATUS.json` 中并阻止 `RELEASED`。
