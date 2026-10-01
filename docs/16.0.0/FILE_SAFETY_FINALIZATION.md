# 司忆 v16 文件安全实施复验

日期：2026-10-01（Asia/Shanghai）。主执行者已将当前源码版本统一为 `16.0.0`，源码 schema 为 `46`，HEAD 为 `7449c43068a8fb0bf5154666547184d2fbf75fa2`，工作区有未提交改动。下述原始文件回归实际运行时 `VERSION` 为 `15.0.0`；保留该测试时间点，最终 v16 门禁须重新绑定统一后的完整源码。

本报告是文件模块源码及隔离测试的交接证据；最终发布必须由统一 RC 检查重新绑定完整源码和候选产物。文件复验任务未迁移用户数据库、未安装应用、未提交 Git；版本统一由主执行者随后完成。

## 本轮结果

- 上轮 `f01-f02-f03-final-regression.log` 中 10/50 项混合批次未到达 `compensated` 的两项失败已复验转绿。`restore_sequence` 先逆序模拟整个恢复计划，并传递本计划自身恢复产生的确切文件身份；每项仍经原批次 grant、Tool、receipt、文件锁及提交前权限/身份检查。
- 1/10/50 项 write/copy/move/delete 混合计划，分别在 backup_verified、effect_started、after_manifest_before_journal、committed、compensation_started、compensated 六个窗口触发真实子进程 `os._exit(73)`；新进程只核对现场，不自动重放，核对前后文件内容一致。
- 两个真实进程共享同一个 operation_id，原子 claim/submit 只允许一次执行；核对已恢复记录仍要求当前现场匹配，外部变化及缺失恢复后证明进入 needs_attention。
- 原四项独立 Windows 探针实际执行，全部 PASS、零 skip。暂存后撤权与父目录置换新增明确的注入到达断言；in-place reparse 探针要求成功设置真实 junction tag，随后清除的仅为测试自建 tag。
- 新发现的 F03 缺口已修复：备份原先直接 bulk copy，复制阶段没有取消点，后续校验使用新预算。现在每 1 MiB 读写检查累计条目/字节/时间/取消预算，独占创建副本并 fsync；备份前扫描、复制、备份校验、源现场复核共用一个预算。失败保留 partial 副本，尚未开始工作区修改。

备份扫描、复制和校验合计沿用 4 GiB/10,000 条目/30 秒预算，批次预检沿用 8 GiB 累计扫描预算。文本编辑仍有 20 MiB 上限，二进制列出/移动不依赖全文解码。复制在每块之间协作取消；操作系统内部单次 I/O 阻塞不能据此承诺绝对 2 秒截止。

## 本轮命令与原始结果

| 检查 | 结果 | 原始文件 |
| --- | --- | --- |
| journal 中真实进程中止/双进程 claim/恢复后 drift 定向复验 | 21 passed、31 deselected；未改变中止码及身份断言 | 本轮终端执行，随后全组完整覆盖相同用例 |
| 备份复制取消、共享预算、部分写入三项先验失败 | 3 failed | `build/v1600-evidence/f03-backup-budget-red.log` |
| 资源边界、恢复、独立探针扩展回归 | 52 passed | `build/v1600-evidence/f03-backup-budget-expanded.log` |
| 文件/权限/锁/快照/Runtime 恢复/Verifier 完整相关组 | 288 passed、3 skipped、1 warning；109.73 秒 | `build/v1600-evidence/f01-f02-f03-final-20261001.log` |
| 原四项 Windows 独立探针，逐项可见 | 4 passed、0 skipped | `build/v1600-evidence/f01-native-review-final-20261001.log` |
| add-function、modify-config、recover-tool-failure、permission-limited、sandbox-escape scripted Runtime Eval | 5/5 passed；零权限/沙箱违规、零无关修改 | `build/v1600-evidence/f01-f02-f03-final-evals-20261001.log`；报告 `build/v1600-evidence/files-final-evals/20261001T113603Z-v16-files-final-20261001-612c1332/report.json` |

相关组命令：

```powershell
.\siyi\.venv\Scripts\python.exe -m pytest tests/backend/tools/test_sandbox.py tests/backend/tools/test_file_operations_v920.py tests/backend/tools/test_file_batch_v150.py tests/backend/tools/test_file_recovery_v160.py tests/backend/tools/test_file_recovery_review_v160.py tests/backend/tools/test_file_journal_v160.py tests/backend/tools/test_file_journal_migration_v160.py tests/backend/tools/test_file_metadata_v160.py tests/backend/tools/test_file_resource_boundaries_v160.py tests/backend/runtime/test_recovery.py tests/backend/runtime/test_startup_recovery_v150.py tests/backend/tools/test_permissions.py tests/backend/security/test_permission_broker_v940.py tests/backend/security/test_admin_action_permissions_v150.py tests/backend/workspace/test_file_locks.py tests/backend/workspace/test_snapshots.py tests/backend/runtime/test_task_verifiers.py tests/backend/runtime/test_verification.py --no-cov -q -p no:cacheprovider
```

三个既有 skip 来自 `test_sandbox.py` 的 Windows 文件/目录符号链接创建权限不足，不属于四项原生恢复探针，也不能作为这些符号链接用例 PASS。唯一 warning 为既有 Starlette TestClient/httpx 弃用提醒。全组未启用覆盖率；80% 全后端门禁由统一 RC 单独执行。

## 原始证据 SHA-256

| 文件 | SHA-256 |
| --- | --- |
| f01-f02-f03-final-20261001.log | `02C774A98054A6B9C70C01EA74694D46EE6E3F7A0D65222893AD05BD4D0D7E92` |
| f01-native-review-final-20261001.log | `6F6F2DAE5A5A834514283C9504ADD29F2C745A69CBACA07B06AA47A2DFDA822E` |
| f03-backup-budget-expanded.log | `2A252BA00CA258B9AFABDBBEB66A329229A6043E086F0861C1A96D089461FE96` |
| f03-backup-budget-red.log | `DD295D8610023C5FA3BEFBC1A009F546654FAA5B92124038536076C898BF2797` |
| f01-f02-f03-final-evals-20261001.log | `D350D17043A04F292E6753E59202FA1B9541F2DB692492AFD8807EAB6E9CD7F0` |

## 文件模块冻结交接

下面是本轮完成后的源码 SHA-256，供主执行者发现后续漂移；总体源码指纹需在各并行模块收尾、版本统一之后重新生成。

| 文件 | SHA-256 |
| --- | --- |
| siyi/app/sandbox.py | `3BCBA92AC50ABE08D5CE0CFB09B651CBFFE2DFDF34854F53FF9D7453B126043B` |
| siyi/app/tools/file_operations.py | `AE29D5D822833516D30C31172AD30B8AEE66DD7D74C8D86B97CA1F7F26258242` |
| siyi/app/tools/batch_plan.py | `474403BC26F1C1F0AD59858E76D80524F9D6479236D5086E13F327D15DD3327C` |
| siyi/app/tools/batch_grants.py | `B4F2166912A6D514BA76FD76208B68A2D2140129E6189D1BB95A1F31157B9396` |
| siyi/app/workspace/file_recovery.py | `DF6F8D6715EA1C5F15947DE517A56C3A81105A3974EB9CFD5B8CA4C1CA97A735` |
| siyi/app/workspace/file_journal.py | `C5B52313CE806C2DF7BA851A2011968C7F9DC7D42CFFC92F06319A8919746C78` |
| siyi/app/workspace/recovery_inventory.py | `B54443152619D1BE655D49568067FFD40668F3D9705B6AC772794F9677D90FC5` |
| siyi/app/workspace/file_locks.py | `CEFFB29F98F70184DFDA3B64D2DE8DA2050AE9E9A7A3B2164F50370E7A389405` |
| siyi/app/tools/receipts.py | `67E3FF9175780F8309CE4C5CF56B959BD46D673831762ECF5B505EF89A1EFA16` |
| siyi/app/database.py | `2EB633EFBB768C28B24015E259334213A7A216236017B3DF677DB17C1C080896` |
| siyi/app/database_modules/file_journal_migration.py | `5675945093BE78691A7A35023AEA715684805D6C3C72C6DD0C156EBB69CD4D2D` |
| tests/backend/tools/test_file_recovery_review_v160.py | `D727824DC7346688F7377E56028F9330764862DF6B280A92BDF682098AA3E934` |
| tests/backend/tools/test_file_journal_v160.py | `95A5AFA120F477D15D6FFDD49F43EB392267A4A716276DFB3E412EAFB0ED0A43` |
| tests/backend/tools/file_journal_crash_worker.py | `A5CFF5A5D11F0A4DA0BF1314F2277EA20F62DFE4E04A9639E41AC6ECBBB5442D` |
| tests/backend/tools/test_file_resource_boundaries_v160.py | `2FC6DE4662B7C1113659228801CD358D4F3A24C773D7EBE64B48B5ECB26CF903` |
| tests/backend/tools/test_file_recovery_v160.py | `CB077BB510C0823C6EFFB8A5F739ADA288B8246BE03B8309D6D459A249B78F2B` |

本轮新增/调整仅为有界备份复制、资源边界四项测试、截断复制故障注入适配新复制入口、独立探针到达断言和本报告。其余 F01/F02 实现由前一轮落地，本轮复验未放宽其授权、身份或崩溃合同。文件线至此冻结，后续由主执行者完成整体测试和最终版本/产物绑定。
