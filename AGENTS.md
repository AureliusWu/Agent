# Repository Guidelines

## Agent Working Rules

Operate only inside the user-selected workspace. Preserve the three permission modes: `ask` requests approval for writes, `agent` may perform ordinary workspace edits, and `full` permits workspace file changes while still confirming critical commands and external MCP calls. Never weaken path normalization, symlink checks, process API authentication, capability binding, outbound network policy, prompt-injection handling, audit logging, snapshots, or secret handling.

## Project Structure

Backend code lives in `backend/app/`. Keep `main.py` limited to application assembly, HTTP endpoints in `routes/`, orchestration in `task_runner.py`, controlled child scheduling in `multi_agent.py`, cross-task write ownership in `file_locks.py`, executable tool policy in `runtime_tools.py`, and domain logic in focused modules such as `sandbox.py`, `recovery.py`, `provider.py`, `permissions.py`, `verification.py`, `memory.py`, `mcp.py`, and `skills.py`. Trust boundaries belong in `request_security.py`, `trust.py`, `network_security.py`, `data_flow.py`, and `snapshots.py`; do not duplicate or bypass them. Backend tests use `backend/tests/test_<feature>.py`.

Agent planning, evidence verification, and bounded repair live in `planning.py`, `verification.py`, and `repair.py`. Structured conversation context and trust-aware memory live in `context.py` and `memory.py`. Model selection and pricing belong in `model_routing.py`; Token budgets, result compaction, safe parallelism, and read caching belong in `efficiency.py`; cached stack detection belongs in `environment.py`. The Executor must never write `completed` directly or pass its own narrative into the Verifier. Agent evaluation code lives in `backend/app/evals/`; fixed contracts and the stable-release policy live in `backend/evals/`. Keep generated reports under ignored `data/evals/`, never in source control.

Frontend code lives in `frontend/src/`. Put reusable UI in `components/`, stateful behavior in `hooks/`, and component styles in `styles/`. The Windows shell is under `frontend/src-tauri/`. Do not rebuild a large all-purpose `App.tsx` or global stylesheet.

## Verification Commands

Run the repository workflows from PowerShell:

```powershell
.\scripts\dev.ps1       # Start FastAPI and Vite; creates backend/.env when absent
.\scripts\test.ps1      # Backend tests, frontend lint, and production build
.\scripts\eval.ps1      # Run the fixed 18-task Agent evaluation suite
.\scripts\clean.ps1     # Remove reproducible build artifacts
```

For desktop changes, also run `cd frontend; cargo check --manifest-path src-tauri/Cargo.toml`. A release is not complete until the relevant API, PWA, and cancellation path have been exercised.

## Coding And Tests

Use Python 3.12, type hints, four-space indentation, and UTF-8. Use TypeScript strict mode, functional React components, and descriptive `PascalCase` component names. Keep public API behavior backward compatible unless the change is documented.

Every behavior change needs a focused test. Backend coverage must remain at or above 70%. Mock network providers and MCP processes; never use a real credential in tests. Security tests must cover cross-workspace/task token replay, SSRF and metadata denial, cross-origin credential redirects, prompt injection, provider redaction, MCP credential blocking, metadata-only data-flow logs, and snapshot restoration. Multi-Agent tests must prove child write denial, file-scope enforcement, bounded depth/concurrency/Token/time, parent cancellation, independent Verifier revision, parent-child Trace, and conflicting writes stopping without merge. Cancellation tests must prove that an in-flight model wait is interrupted, not merely marked cancelled afterward. Recovery tests must inject a stop after a side effect and prove that resume neither repeats the operation nor hides workspace drift. Context tests must prove required constraints survive compression and that irrelevant Skills, tools, and memories are not injected. Efficiency tests must prove model escalation, per-phase budgets, explicit truncation, cache invalidation, and that only independent read operations run in parallel. Never cache or skip independent verification to save time or cost. Never mark a code task complete without persisted verification evidence.

Changes to the Agent runtime, permissions, tools, context, memory, MCP, Skill, verification, or child scheduling must keep `backend/evals/tasks.json` valid and run the relevant scripted eval. Multi-Agent changes must also run `backend/evals/multi_agent_tasks.json`. Do not weaken a rule to make a regression green. A stable release additionally requires `scripts/release-gate.ps1`; blocked or skipped live-model tasks are not passes.

## Commits And Security

Use Conventional Commits such as `fix: interrupt running agent tasks`. Do not commit `.env`, databases, credentials, generated installers, `build/`, `dist/`, or Rust `target/` output. Update `README.md` for user-facing behavior and this file only when contributor rules change.
