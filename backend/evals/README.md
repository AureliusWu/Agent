# Agent Eval

`tasks.json` is the fixed capability contract introduced in Round 8 and used to verify the Round 9 planning, independent verification, and bounded repair lifecycle. Every task declares type, difficulty, tools, network/write/command permissions, confirmation requirements, time/tool/Token budgets, expected files, validation commands, deterministic rules, and expected outcome.

Three layers share the same isolated workspaces and evidence engine:

- `scripted_runtime` uses deterministic model actions to exercise the real Agent loop, permissions, tools, database, verifier, timeout, cancellation, MCP failure and Sidecar lifecycle.
- `live_model` calls the configured OpenAI-compatible Provider. Missing credentials produce `blocked`, never a pass.
- `adversarial` runs compact deterministic attacks against prompt-injection, approval, sandbox, and honest-completion boundaries.

Run from the repository root:

```powershell
.\scripts\eval.ps1 -Mode scripted_runtime -Label before-change
.\scripts\eval.ps1 -Mode live_model -Label deepseek
.\scripts\eval.ps1 -Mode adversarial -Label security -Suite adversarial -Tasks backend/evals/adversarial_tasks.json
```

To reproduce one or more failures without rerunning a complete suite, pass `-TaskId project-structure,fix-clear-bug`. Ordinary development uses 3-5 representative tasks. Complete core, Multi-Agent, professional, and live-model suites are reserved for milestones, broad runtime/security changes, migrations, and release candidates.

`companion_contracts.json` defines contract-only interfaces for persona consistency, provider-switch identity, memory conflicts, Conversation Mode safety, sensitive-memory writes, and a unified voice/text message protocol. Validate them with `python -m app.evals.cli validate-companion`.

Reports are written to `data/evals/<timestamp>-<label>-<run>/report.json` and `report.md`; `history.jsonl` indexes prior runs. Compare versions with:

```powershell
backend\.venv\Scripts\python -m app.evals.cli compare `
  --baseline <old-report.json> --candidate <new-report.json> `
  --output data/evals/comparison
```

`gate-policy.json` is intentionally strict. A candidate with false completion, unrelated modifications, permission/sandbox violations, a low success rate, or a detected regression cannot be marked stable. Normal development commits may preserve known failing capability tasks; their reports must remain explicit and no stable tag may be created.
