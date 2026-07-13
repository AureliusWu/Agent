# Agent Eval

`tasks.json` is the fixed capability contract for Round 8. Every task declares type, difficulty, tools, network/write/command permissions, confirmation requirements, time/tool/Token budgets, expected files, validation commands, deterministic rules, and expected outcome.

Two modes share the same workspaces and evidence engine:

- `scripted_runtime` uses deterministic model actions to exercise the real Agent loop, permissions, tools, database, verifier, timeout, cancellation, MCP failure and Sidecar lifecycle.
- `live_model` calls the configured OpenAI-compatible Provider. Missing credentials produce `blocked`, never a pass.

Run from the repository root:

```powershell
.\scripts\eval.ps1 -Mode scripted_runtime -Label before-change
.\scripts\eval.ps1 -Mode live_model -Label deepseek
```

To reproduce one or more failures without rerunning the full paid suite, pass `-TaskId project-structure,fix-clear-bug`. The default remains the complete 18-task suite.

Reports are written to `data/evals/<timestamp>-<label>-<run>/report.json` and `report.md`; `history.jsonl` indexes prior runs. Compare versions with:

```powershell
backend\.venv\Scripts\python -m app.evals.cli compare `
  --baseline <old-report.json> --candidate <new-report.json> `
  --output data/evals/comparison
```

`gate-policy.json` is intentionally strict. A candidate with false completion, unrelated modifications, permission/sandbox violations, a low success rate, or a detected regression cannot be marked stable. Normal development commits may preserve known failing capability tasks; their reports must remain explicit and no stable tag may be created.
