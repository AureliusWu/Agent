# 司忆 v8.0.2 Phase 4 任务租约、队列与凭据恢复报告

记录时间：2026-07-27

## 当前结论

Phase 4 已完成，精确源码提交为 `b5b74acd79b60ed578819bdc5c0bbf4a5a4505c5`。绑定该提交的 GitHub CI 已成功，因此 `V802-LEASE-001~004` 与 `V802-CRED-001~002` 均可写为 `PASS`。产品版本仍为 `8.0.1`，整体发布状态仍为 `NOT_READY`。

## 实现边界

- 任务在任何 `RUNNING` 状态迁移前先原子获取租约，再以 `expected_status + lease_generation` 做 CAS。
- 竞争实例只返回 `409/owned_by_other_runtime`，不覆盖持有者状态、不写终态事件、不停止持有者。
- `agent_tasks`、`task_operations`、`tool_runs`、`task_checkpoints`、`task_events` 和副作用回执均绑定租约代次；旧代次写入被拒绝或抑制。
- 已完成副作用保留原始代次；只有仍在运行、不确定、待确认或已取消的操作可由新租约接管。
- 队列 Claim 记录实例、进程、代次与到期时间；只有当前 Claim 所有者可完成，活跃租约存在时不回收。
- STOP 在单一写事务内撤销活跃租约并写入取消状态，旧执行代次不能随后覆盖终态。
- 任务只持久化凭据来源、Provider profile、所需能力与不可逆绑定哈希，不持久化明文凭据。
- 请求头凭据在重启后缺失时进入独立的 `waiting_provider_credential` 状态，重新授权后继续；Provider profile 漂移被显式拒绝。
- Windows Credential Manager 仍是桌面凭据恢复来源，前端恢复入口会重新读取本地秘密。

## Schema 说明

计划原定 Schema v30。由于开发分支上的第一批租约列已以 v30 推送，为避免已经运行过中间 v30 的数据库永远跳过后续列，本次将最终兼容迁移记为 v31。v31 重跑幂等的 v30 列检查，不修改既有迁移历史，仍经过迁移前备份与失败自动恢复路径。

## 本地实际运行证据

聚焦命令：

```powershell
.\siyi\.venv\Scripts\python.exe -m pytest tests/backend/runtime/test_recovery.py::test_crash_after_file_write_recovers_without_duplicate_side_effect tests/backend/runtime/test_task_leases.py tests/backend/runtime/test_task_runner.py -q
```

结果：`37 passed`。其中 Windows `multiprocessing spawn` 实际启动两个进程竞争同一 Resume，模型执行记录恰为一次。

全量命令：

```powershell
.\scripts\test.ps1
```

结果：后端 `404 passed, 1 skipped`，覆盖率 `82.30%`；前端 lint、TypeScript/Vite 构建、安全扫描与推理边界测试均通过。

桌面命令：

```powershell
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

结果：`5 passed`，包含 Windows Credential Manager 往返测试。

脚本化 Eval（不调用真实 Provider）：

- Core：`18/18` 通过，run `c05963bc06e7488ab32e648c004bd62f`。
- Adversarial：`4/4` 通过，run `6f05429401dd482bbd3faf69412f93aa`。
- Multi-Agent：`0/3`，未通过；v8.0.1 运行时和锁定单测会把旧编排请求统一降级为 `single`，但旧评测仍要求子 Agent，属于基线契约冲突，不计为 PASS。
- Professional Agent：`0/4`，未通过；v8.0.1 将旧专业 profile 映射到 `general`，但旧评测仍要求专业 profile，属于基线契约冲突，不计为 PASS。

## 数据可靠性结论

当前判定为 `CONDITIONALLY TRUSTED`：迁移前备份、失败恢复、代次账本、明文凭据零持久化与重新授权路径已有自动化证据；安装包升级和隔离 AppData 的完整桌面 E2E 尚未在本阶段执行。

## CI 与用例状态

首次 GitHub CI run `30261820927` 在后端队列测试中失败：测试把排队等待和任务完成等待错误地共用一个 4 秒截止时间，慢速 runner 在释放首任务时截止时间已耗尽。修正后该用例本地连续运行三次通过。

最终 GitHub CI：run `30262466688`，绑定精确提交 `b5b74acd79b60ed578819bdc5c0bbf4a5a4505c5`，结论 `success`，耗时 12m18s。CI 实际执行后端、评测契约、紧凑对抗门禁、前端、Windows 桌面壳与 SBOM。

`V802-LEASE-001~004` 与 `V802-CRED-001~002` 已绑定具体测试名、精确提交和上述 CI run，详细映射见 `TEST_MATRIX.json`。

## 未完成与发布影响

- Multi-Agent 与 Professional Agent 的旧评测契约需要在 Phase 8/release gate 前做明确收敛；当前结果是 `FAIL`，不是跳过或成功。
- 安装包升级、隔离 AppData、SSE 重连、真实桌面 STOP 一致性属于后续桌面阶段。
