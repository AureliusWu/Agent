# Real Scenarios

`catalog.json` 是 v8.0.1 的 18 个真实场景定义。实际运行结果写入仓库根目录下被忽略的 `v8.0.1-release-evidence/<run-id>/<scenario-id>/`。

每个场景结果必须包含：

- `scenario.json`
- `events.jsonl`
- `tool-receipts.jsonl`
- `token-ledger.json`
- `screenshots/`
- `artifacts/`
- `final-report.md`

运行前先验证定义：

```powershell
.\siyi\.venv\Scripts\python.exe scripts\validate-real-scenarios.py
```

验证一次实际运行：

```powershell
.\siyi\.venv\Scripts\python.exe scripts\validate-real-scenarios.py --run-root v8.0.1-release-evidence\<run-id>
```

验证器不会把目录存在或代码存在判定为 PASS。任何 PASS 都必须具有成功命令、时间、Build ID 和实际证据引用。
