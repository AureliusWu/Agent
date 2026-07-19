# Repository Guidelines

## Agent Working Rules

Operate only inside the user-selected workspace. Use `AGENT_ROADMAP_V3_PERSONAL_COMPANION.md` as the companion baseline and `docs/V4_0_0_REQUIREMENTS_MATRIX.md` as the current runtime capability boundary. Implement one release target at a time. Preserve the three permission modes: `ask` requests approval for writes, `agent` may perform ordinary workspace edits, and `full` permits workspace file changes while still confirming critical commands and external MCP calls. Never weaken path normalization, symlink checks, process API authentication, capability binding, outbound network policy, prompt-injection handling, audit logging, snapshots, or secret handling.

The current product target is the Windows PC desktop MVP. Treat Tauri, the packaged FastAPI sidecar, Credential Manager, installed-app lifecycle, and `司忆.exe` as the primary delivery path. Keep web code buildable but frozen: do not add web-only features, mobile adaptation, cloud sync, or browser credential persistence unless the roadmap explicitly resumes them. Preserve platform-neutral API and runtime modules so web support can return later.

## Project Structure

Executor contracts and the trusted Windows implementation live in `siyi/app/executor.py`. Runtime orchestration must use the registered Executor instead of importing local file or command functions directly.

SQLite is the authoritative source for the conversation queue. Keep queue persistence and priority operations in `queue_service.py`, worker wake-up and single-conversation scheduling in `task_runtime.py`, safe-point steering in `task_runner.py`, and parent-child cancellation in `cancellation.py`. Submit, steer, promote, cancel queued, and stop are distinct operations; do not collapse them into a shared status toggle or frontend-only state.

Backend code lives in `siyi/app/`. Keep `main.py` limited to application assembly, HTTP endpoints in `routes/`, orchestration in `task_runner.py`, controlled child scheduling in `multi_agent.py`, cross-task write ownership in `file_locks.py`, executable tool policy in `runtime_tools.py`, domain verification in `task_verifiers.py`, and shared report orchestration in `verification.py`. Lifecycle extension points belong in `hooks.py`, LSP protocol handling in `lsp.py`, reusable MCP sessions in `mcp.py`, and isolated Git worktrees in `worktrees.py`; these modules must remain behind the Executor, permission, audit, and cancellation boundaries. Other domain logic belongs in focused modules such as `sandbox.py`, `recovery.py`, `provider.py`, `permissions.py`, `memory.py`, and `skills.py`. Professional profiles belong in `agent_profiles.py`; declarative package validation and lifecycle belong in `extension_sdk.py` and `extensions_runtime.py`. Trust boundaries belong in `request_security.py`, `trust.py`, `network_security.py`, `data_flow.py`, and `snapshots.py`; do not duplicate or bypass them. Backend tests use `tests/siyi/test_<feature>.py`.

Workspace fallback code intelligence belongs in `workspace_index.py`. Keep it read-only, rebuildable, bounded by file and output limits, and scoped to the canonical selected workspace. `lsp.py` may present results as language-server evidence only after a successful ordered JSON-RPC handshake; when no server is installed it must label the result as a workspace-index fallback. Managed worktrees must stay under `.agent/worktrees`, remain excluded from indexing, and use the existing critical-operation confirmation path for removal.

Agent planning, evidence verification, and bounded repair live in `planning.py`, `verification.py`, and `repair.py`. Conversation compaction remains in `context.py`; model-window accounting belongs in `context_budget.py`; authoritative layer assembly and memory-reference tracking belong in `context_assembler.py`. The immutable identity contract lives in `identity_kernel.py` with version management in `identity.py`; affect and relationship state live in `affect.py`; canonical long-term memory, consolidation/continuity, and complete backup logic live in `long_term_memory.py`, `memory_consolidator.py`, and `full_backup.py`. The older `memory.py` remains a compatibility layer for engineering memory and must not become a second personal identity store. Model selection and pricing belong in `model_routing.py`; adaptive Token budgets, result compaction, safe parallelism, and read caching belong in `efficiency.py`; cached stack detection belongs in `environment.py`. Budgeting must combine a global safety ceiling with model context limits, per-call output caps, round/tool limits, and preflight input estimates. Do not introduce a small fixed task cap as the primary cost control. The Executor must never write `completed` directly or pass its own narrative into the Verifier. Agent evaluation code lives in `siyi/app/evals/`; fixed contracts and the stable-release policy live in `evals/`. Keep generated reports under ignored `data/evals/`, never in source control.

Provider capability observations belong in `provider_capabilities.py`; OpenAI-compatible request/response handling stays in `provider.py`. Keep client protocol support separate from provider-observed support, never probe with paid model calls automatically, and never store API keys or raw credential-bearing endpoints in capability records. Data-driven routing needs a minimum sample threshold, while explicit model and reasoning choices remain authoritative.

`VERSION` is the release version source. Keep backend, npm, Cargo, Tauri, and lock metadata synchronized and run `scripts/check-release-metadata.py`. Install Python from `siyi/requirements.lock`, npm with `npm ci`, and Cargo with `--locked`. Database migrations require a pre-migration backup and tested failure restoration. Desktop release smoke must use `AGENT_DESKTOP_DATA_DIRECTORY` with a test-owned temporary directory, launch the installed application, verify migration and process cleanup, and never touch the user's real application data. Diagnostic bundles must pass through `trust.py` redaction and must never include databases, workspace files, environment files, credentials, or full machine paths.

Build provenance belongs in `scripts/generate_build_info.py`, `siyi/app/build_info.py`, `desktop/frontend/src/buildInfo.ts`, and the Tauri build command. Generate one locked manifest per release build and inject it into every component. Never query Git from an installed application, hand-edit build IDs, omit dirty source fingerprints, or let a failed diagnostics refresh retain stale Sidecar identity. Release tests must prove current-component agreement and deliberate old-Sidecar mismatch detection.

Keep project and personal memory namespaces separate. Runtime retrieval and Agent memory tools use only the project namespace; personal memory is user-managed API data and may be imported or exported without a workspace. Never silently move records between namespaces. Enforce `deny`, `explicit`, and `allow` write policies at the Runtime boundary, and never auto-store task experience unless the policy is `allow`.

Frontend code lives in `desktop/frontend/src/`. Put reusable UI in `components/`, stateful behavior in `hooks/`, and component styles in `styles/`. The Windows shell is under `desktop/src-tauri/`. Do not rebuild a large all-purpose `App.tsx` or global stylesheet.

## Verification Commands

Run the repository workflows from PowerShell:

```powershell
.\scripts\dev.ps1       # Start FastAPI and Vite; creates siyi/.env when absent
.\scripts\test.ps1      # Backend tests, frontend lint, and production build
.\scripts\eval.ps1      # Run the fixed 18-task Agent evaluation suite
.\scripts\smoke-sidecar.ps1 # Verify the packaged backend and clean its process tree
.\scripts\build-desktop.ps1 # Build and smoke NSIS/MSI packages and generate the SBOM
.\scripts\clean.ps1     # Remove reproducible build artifacts
```

For desktop changes, also run `cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml`. A Windows release is not complete until the installed desktop app starts, its isolated sidecar stops cleanly, upgrade data survives, locked metadata agrees, and the real release workflow has uploaded artifacts.

## Coding And Tests

Use Python 3.12, type hints, four-space indentation, and UTF-8. Use TypeScript strict mode, functional React components, and descriptive `PascalCase` component names. Keep public API behavior backward compatible unless the change is documented.

Persistent-runtime changes must prove task creation returns before model completion, same-conversation work is serialized, terminal events persist, SSE resumes from `Last-Event-ID`, refresh can reattach, and shutdown preserves resumability without replaying side effects.

Planner changes must test semantic JSON parsing, deterministic fallback, schema validation, policy enforcement, workspace boundaries, capability narrowing, and persisted Task Contract round-trips.

Every behavior change needs a focused test. Backend coverage must remain at or above 70%. Mock network providers and MCP processes; never use a real credential in tests. Every acceptance item must expose a stable `requirement_id`; Code, API, UI, Database, Security, and Document requirements must be checked by the matching specialized Verifier, and unrelated successful commands must not satisfy them. Security tests must cover deployment-mode authentication, non-loopback startup denial, browser credential persistence, frontend build-secret scanning, cross-workspace/task token replay, SSRF and metadata denial, cross-origin credential redirects, prompt injection, provider redaction, MCP credential blocking, metadata-only data-flow logs, and snapshot restoration. Multi-Agent tests must prove child write denial, file-scope enforcement, bounded depth/concurrency/Token/time, parent cancellation, independent Verifier revision, parent-child Trace, and conflicting writes stopping without merge. Extension tests must prove manifests cannot lower risk, delegate critical tools, embed credentials, escape package paths, bypass approvals, or survive tampering; dependencies, profile snapshots, failure isolation, upgrade, disable, and rollback must be deterministic. Cancellation tests must prove that an in-flight model wait is interrupted, not merely marked cancelled afterward. Recovery tests must inject a stop after a side effect and prove that resume neither repeats the operation nor hides workspace drift. Context tests must prove required constraints survive compression and that irrelevant Skills, tools, and memories are not injected. Efficiency tests must prove model escalation, per-phase budgets, explicit truncation, cache invalidation, and that only independent read operations run in parallel. Never cache or skip independent verification to save time or cost. Never mark a code task complete without persisted verification evidence.

Changes to the Agent runtime, permissions, tools, context, memory, MCP, Skill, verification, professional profiles, extensions, or child scheduling must keep `evals/tasks.json` valid and run the relevant scripted eval. Multi-Agent changes must also run `evals/multi_agent_tasks.json`; profile or extension changes must run `evals/professional_agent_tasks.json`. Do not weaken a rule to make a regression green. A stable release additionally requires `scripts/release-gate.ps1`; blocked or skipped live-model tasks are not passes.

Use tiered verification and publishing. For narrow local changes, run focused unit tests plus 3-5 representative scripted Eval tasks; keep the work local and accumulate it. Run the complete core, Multi-Agent, and professional suites for roadmap milestones, broad runtime or contract changes, security/permission changes, database migrations, or release candidates. Commit and push milestone-sized or user-requested releases, not every small iteration.

## Commits And Security

Use Conventional Commits such as `fix: interrupt running agent tasks`. Do not commit `.env`, databases, credentials, generated installers, `build/`, `dist/`, or Rust `target/` output. Update `README.md` for user-facing behavior and this file only when contributor rules change.
