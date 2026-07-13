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

## Implemented in Round 9

- **Persistent planning:** every task receives an explicit goal, dependency-ordered steps, risks, expected paths, and deterministic acceptance criteria before execution.
- **Independent verification:** the Verifier receives the original task and a sanitized evidence package containing final file state, command results, key tool outcomes, failures, and side effects. It never trusts the Executor's completion claim.
- **Verifier-owned completion:** the Executor cannot write `completed`; only a passed verification report can finalize that state. Unsupported capabilities produce `blocked` without false success.
- **Bounded repair:** failed criteria can trigger at most two scoped repair attempts. Passed criteria are protected from repeated mutation, every attempt is persisted, and no-progress repair stops automatically.
- **Auditable roles:** plans, verification attempts, and repair runs are available through the task trace API and existing audit interface.

## Implemented in Round 10

- **Durable checkpoints:** task phase, cursor, pending tool calls, messages, changed files, commands, failures, verification state, and workspace evidence persist in SQLite.
- **Pause and exact resume:** running work can be paused and resumed from a selected checkpoint without asking the model to recreate the pending action.
- **Workspace-drift guard:** Git state, dependency files, expected paths, and workspace inventory are compared before resume; changed workspaces require explicit confirmation.
- **Idempotent side effects:** every operation has a stable execution ID. Completed file mutations are recovered from their backup manifests instead of being executed twice.
- **Recovery controls and audit:** the PWA lists recoverable tasks, checkpoint reasons, uncertain operations, and explicit continue or abandon actions.

## Implemented in Round 11

- **Layered context:** each model round receives a bounded current-context block and persisted working memory covering the active goal, phase, plan, progress, failures, constraints, dependencies, risks, and verification state.
- **Structured compaction:** conversation compression preserves ten required fields instead of relying on unconstrained prose, and the summary remains explicitly subordinate to current safety and permission rules.
- **On-demand capability loading:** built-in tools, MCP definitions, Skills, project memories, and experience memories are selected by plan and relevance budgets rather than injected in full.
- **Trust-aware memory:** memory records carry provenance, version, project signature, confidence, verification time, use/success/failure counts, and rejection state. Framework, dependency, structure, age, failures, and user feedback lower effective confidence.
- **Memory management:** the PWA and API support viewing, creating, editing, verifying, rejecting, and deleting project or experience memory. Only tasks that recover from an error and pass independent verification may create automatic experience memory.

## Implemented in Round 12

- **Configurable model routing:** lightweight, medium, and strong tiers are selected from task intent. Provider failures and verifier-requested repairs escalate one tier at a time; every decision is recorded.
- **Layered budgets and cost trace:** task, phase, and single-call Token limits are enforced independently. Input/output Token, latency, route tier, and optional configured USD estimates are visible for each task and phase.
- **Evidence-preserving compaction:** command logs, file snippets, search matches, JSON, and MCP results are bounded before model injection. Truncation is explicit while the full sanitized result remains in the audit database.
- **Safe concurrency and caching:** only independent built-in read calls may execute concurrently. Task-local read results, Skill files, project signatures, and build-environment detection are cached with write- and signature-based invalidation.
- **Cost policy API:** model tiers, budgets, cache settings, and configured pricing model names are inspectable without exposing credentials.

## Deliberately Deferred

- **Sub-agents and parallel writes:** require an explicit delegation model, shared-budget rules, and conflict handling before multiple workers can safely write to one workspace. Round 12 parallelism is limited to independent reads.
- **Plugin marketplace:** requires package signing, provenance checks, version pinning, and an isolation policy. Local Skills and MCP remain the supported extension paths.
- **Scheduled tasks:** require explicit unattended-execution permissions, credential availability, retry limits, and a Windows background-service decision.

These deferred items are not placeholders or hidden switches. They should be implemented only after their trust and operating policies are decided.
