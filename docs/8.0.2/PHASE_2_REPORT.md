# 司忆 v8.0.2 Phase 2 推理安全报告

记录日期：2026-07-27（Asia/Shanghai）

## 当前结论

原始 Provider reasoning 已从可观察和持久化边界中切断，本地全量回归通过，远程 CI 待提交后验证。v8.0.2 新增矩阵在 CI 绑定当前提交前继续保持 `NOT_RUN`，不按代码存在性提前判定 PASS。

## 边界设计

- Provider 流式响应中的原始 `reasoning_content` 只写入进程内私有字段。
- DeepSeek 原生工具调用需要回放时，仅在 Provider 出站边界临时恢复协议字段。
- SSE 只发送 `reasoning.summary`，内容来自按 phase 固定映射的公开摘要。
- Runtime 的 API 结果、SQLite message、task event 与 checkpoint 只包含公开摘要。
- 前端不再处理 `model.reasoning.delta`；未知旧事件不会进入推理时间线。
- 日志捕获与诊断包中不得出现私有哨兵。

## 哨兵证据

测试注入：

```text
PRIVATE_CHAIN_OF_THOUGHT_SENTINEL
```

聚焦命令：

```powershell
siyi\.venv\Scripts\python.exe -m pytest tests\backend\cognition\test_reasoning_summary.py tests\backend\providers\test_provider.py tests\backend\runtime\test_task_runner.py::test_workspace_free_conversation_can_chat_and_persist_reasoning tests\backend\runtime\test_task_runner.py::test_private_reasoning_never_enters_checkpoint_events_or_messages tests\backend\runtime\test_task_runner.py::test_workspace_free_conversation_timeout_is_persisted -q
npm.cmd --prefix desktop\frontend run test:security
```

结果：后端聚焦测试 `20 passed`；前端安全测试确认原始 delta 被拒绝、公开摘要可渲染且不会重复。

## 全量本地回归

命令：

```powershell
.\scripts\test.ps1
```

结果：

- 后端：`391 passed, 1 skipped`
- 覆盖率：`82.20%`
- 前端 lint：PASS
- TypeScript / Vite：PASS
- 前端安全与推理事件边界：PASS

## 未完成

- 当前 Phase 2 提交尚未取得远程 CI 证据。
- UI 的真实桌面 DOM/E2E 哨兵验证留在桌面 E2E 阶段，当前只有前端事件到可见时间线状态的自动化边界测试。

