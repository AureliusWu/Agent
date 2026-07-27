# 司忆 v8.0.2 Phase 0 报告

记录日期：2026-07-27（Asia/Shanghai）

## 结论

Phase 0 的本地迁移与验证已完成，当前状态为：

```json
{
  "implementation_status": "MIGRATING",
  "test_status": "LOCAL_PASS_CI_PENDING",
  "distribution_status": "NOT_DISTRIBUTED"
}
```

版本仍为 `8.0.1`。在后续 P0/P1 门禁、完整测试矩阵和最终报告完成前，不更新为 `8.0.2`，不创建 Release，也不合并 Draft PR。

## 干净迁移

- 新分支：`clean/v8.0.2`
- 基线：`origin/main` / `290cfa6ce32c6a29e2eadb3cd137bf844ce910ef`
- 旧分支：`codex/v2.0.1`，仅作只读审计参考，未 merge、未 rebase。
- 私人图片 `natsume-kokoro.png` 未进入新分支历史；源码改用 `kokoro-placeholder.svg`。
- 私人图片副本已保存到 AppData 用户资源目录，不纳入 Git。
- 旧 v8.0.1 本地发布证据已迁出仓库，且不复用为 v8.0.2 测试证据。

迁移提交按域拆分：

| 提交 | 内容 |
|---|---|
| `2bbbe32` | 净化后的 v8 源码树 |
| `5260784` | 测试、门禁脚本与 CI 配置 |
| `ea46733` | Phase 0 基线文档与机器可读基线 |

## 隐私门禁

以下命令均在 `clean/v8.0.2` 上实际运行并通过：

```powershell
python scripts/privacy_scan.py --tracked
python scripts/privacy_scan.py --history
python scripts/privacy_scan.py --scan-path .
python -m pytest tests/backend/security/test_privacy_scan.py -q
```

结果：隐私扫描三项均为 PASS；隐私扫描器测试 `8 passed`。`--history` 只扫描当前发布分支的可达历史；新增的 `--history-all-refs` 用于明确审计整个仓库引用。旧审计分支仍包含已知私人 Blob，因此不得把全引用审计失败误报为新分支失败，也不得把旧分支合并到发布分支。

附加验证：

```powershell
git merge-base --is-ancestor origin/main HEAD
git rev-list HEAD --objects | Select-String natsume-kokoro.png
```

前者退出码为 0；后者无匹配。

## 本地回归证据

### 主测试入口

命令：

```powershell
.\scripts\test.ps1
```

结果：PASS。

- 发布元数据：当前 7 个版本源一致，为 `8.0.1`
- 后端：`387 passed, 1 skipped`
- 覆盖率：`82.09%`
- 前端 oxlint：PASS
- TypeScript 与 Vite 构建：PASS
- 安全检查：PASS（62 个源码文件、9 个构建文件）

### Rust / Tauri

首次运行：

```powershell
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml
```

首次结果：FAIL，原因是净化可再生构建目录后，Tauri `externalBin` 要求的本地 sidecar 尚不存在。这不是测试用例失败，但按实际失败记录保留。

随后实际运行：

```powershell
.\scripts\build-runtime.ps1
cargo test --locked --manifest-path desktop\src-tauri\Cargo.toml
```

重建成功；复测结果：`5 passed, 0 failed`，文档测试与其他测试目标均通过。编译器仅报告 Windows 链接器信息级警告。

## 首次 CI 结果与修复

Draft PR `#7` 的首次 CI run `30251269062` 实际执行后失败。唯一失败项为：

```text
tests/backend/runtime/test_task_runtime.py::test_background_task_returns_before_model_finishes_and_persists_events
```

该测试已确认后台响应边界、模型未在 HTTP 202 响应前结束；失败发生在释放 Mock Provider 后，GitHub 共享 Windows runner 未能在原固定 3 秒轮询窗内把任务状态推进到 `completed`。本地同一测试通过。修复保留真实完成断言，先要求 Mock Provider 在 10 秒内结束，再给后台持久化一个独立、仍有上限的 10 秒窗口，避免把 runner 调度延迟伪装成产品失败。修复后的 CI 必须重新实际运行，首次失败记录不删除。

## 未完成事项

- Draft PR 尚未创建。
- 首次远程 CI 为 FAIL；修复后的复跑证据尚未产生。
- Phase 1 及后续功能、矩阵、P0/P1 门禁均未开始或未完成。
- 当前不具备 `8.0.2` 发布条件。
