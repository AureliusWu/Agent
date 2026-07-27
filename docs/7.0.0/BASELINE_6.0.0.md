# 6.0.0 Baseline

## Scope

The 7.0.0 work starts from the released 6.0.0 continuous-runtime baseline. The product version remains 6.0.0 until every 7.0.0 release gate passes.

## Verified Baseline

- Desktop: Tauri 2 with a Python FastAPI sidecar.
- Frontend: React and TypeScript.
- Persistence: SQLite schema 25.
- Runtime: execution segments, context compaction, tool scheduling, recovery, permissions, audit, Skills, Hooks, MCP, and web search.
- Supported repository instructions: layered `AGENTS.md` and `AGENTS.override.md`; `README.md` remains non-authoritative unless referenced.

## Baseline Risks

- Runtime paths were inconsistent between desktop, sidecar, and development commands.
- The production database used the runtime root instead of `data/`.
- Ignored development databases, logs, evaluations, and workspace backups remained inside the source checkout.
- Several 7.0.0 P0 contracts are absent or incomplete: task-scoped lock leases, durable process supervision, mutation receipts, expected-version checks, and explicit provider quota states.

## Version Gate

Do not change Python, npm, Cargo, Tauri, or root version metadata to 7.0.0 until privacy, migration, recovery, stress, packaging, and upgrade tests are all green.
