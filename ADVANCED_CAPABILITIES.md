# Advanced Capabilities

## Implemented in Round 7

- **Workspace memory:** the Agent can list, save, update, and remove durable facts bound to the currently selected workspace. Memory is stored in SQLite, size-limited, permission-gated, and injected as untrusted reference material.
- **Skill observability:** Skill content is loaded only when the task matches its name or description. Every selected Skill is recorded against the task and shown in the execution trace.
- **Automatic task evaluation:** deterministic checks score each completed task from real file hashes, command results, and unresolved failures. This score never replaces the completion status.

## Implemented in Round 8

- **Repeatable Agent Eval:** 18 fixed tasks run through the real task runner, permission system, workspace sandbox, tool registry, SQLite trace, and deterministic verifier.
- **Evidence-backed reports:** every run produces JSON and Markdown reports with metrics, file hashes, commands, tool/model traces, failed steps, and historical records.
- **Version comparison:** reports can be compared across versions, models, prompts, tool settings, permission modes, budgets, and memory strategies.
- **Stable release gate:** false success, unrelated changes, permission violations, sandbox violations, and detected regressions can block a release from being marked stable.
- **Provider validation:** the same suite can use an OpenAI-compatible live model without persisting the API key in reports, logs, or the database.

## Deliberately Deferred

- **Independent verification and repair:** the current verifier is deterministic but still runs inside the execution process. Round 9 must separate planning, execution, verification, and bounded repair.
- **Interrupted-task recovery:** pending confirmations persist, but execution currently restarts model planning after approval instead of resuming from a durable cursor.

- **Sub-agents and parallel execution:** require an explicit delegation model, shared-budget rules, and conflict handling before they can safely write to one workspace.
- **Automatic model routing:** requires at least one additional Provider configuration and a user-approved cost/quality policy. Routing a single configured model would be cosmetic.
- **Plugin marketplace:** requires package signing, provenance checks, version pinning, and an isolation policy. Local Skills and MCP remain the supported extension paths.
- **Scheduled tasks:** require explicit unattended-execution permissions, credential availability, retry limits, and a Windows background-service decision.

These deferred items are not placeholders or hidden switches. They should be implemented only after their trust and operating policies are decided.
