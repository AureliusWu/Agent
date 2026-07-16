# Project History

This file consolidates the superseded audit, roadmap, and per-version acceptance reports that previously lived in the repository root. Current product requirements remain in `AGENT_REQUIREMENTS.md`; the active implementation order remains in `AGENT_ROADMAP_V3_PERSONAL_COMPANION.md`.

## Milestones

| Version | Milestone | Accepted outcome |
| --- | --- | --- |
| v0.5.0 | Initial capability audit | Established the workspace sandbox, Agent loop, permission modes, and explicit capability backlog. |
| v0.6.0 | Agent Eval | Added fixed task contracts, deterministic reports, live-model sampling, and regression comparison. |
| v0.7.0 | Independent verification | Separated Planner, Executor, Verifier, and bounded repair responsibilities. |
| v0.8.0 | Recovery | Added checkpoints, pause/resume, drift detection, and side-effect replay prevention. |
| v0.9.0 | Context and memory | Added layered context, bounded retrieval, compression, confidence, and expiry. |
| v0.10.0 | Efficiency | Added model routing, escalation, Token budgets, result compaction, safe read parallelism, and caches. |
| v0.11.0 | Security | Added capability-bound approvals, SSRF controls, prompt-injection handling, data-flow audit, snapshots, and rollback. |
| v0.12.0 | Controlled Multi-Agent | Added depth-one child Agents, budgets, file scopes, cancellation, Trace, and conflict stopping. |
| v0.13.0 | Professional Agents and extensions | Added constrained profiles and declarative, non-executable extension packages with validation and rollback. |
| v0.13.1 | Security hotfix | Closed deployment authentication, origin, browser credential, and cross-scope token gaps. |
| v0.14.0 | Persistent runtime | Added durable task events, SSE reattachment, serialized conversations, and interruptible model waits. |
| v0.15.0 | Semantic Planner | Added structured Task Contracts, deterministic fallback, policy validation, and persistence. |
| v0.16.0 | Specialized Verifiers | Bound Code, API, UI, Database, Security, and Document requirements to matching evidence. |
| v0.17.0 | Eval and CI gate | Added stable requirement IDs, CI contracts, and release-blocking evidence rules. |
| v0.18.0 | Executor abstraction | Made `LocalWindowsExecutor` the single trusted runtime writer behind a stable contract. |
| v0.19.0 | Workspace index | Added bounded, rebuildable Python/TypeScript/JavaScript/Rust navigation and source fingerprints. |
| v0.20.0 | Hybrid project memory | Added project-scoped retrieval, trust metadata, invalidation, and explicit write policies. |
| v0.21.0 | Provider routing | Added capability observations, sample-gated routing, manual model overrides, and cost Trace. |
| v0.22.0 | Release engineering | Added locked dependencies, metadata checks, migration backup/restore, diagnostics, NSIS/MSI smoke, and SBOM. |
| v1.0.0 | Windows stable baseline | Accepted fourteen product promises without expanding filesystem or deployment permissions. |
| v2.0.0 | PC desktop MVP | Made the desktop app the primary product and hardened sidecar lifecycle, workspace setup, credentials, task recovery, status reporting, character assets, and release naming. |

## v1.0.0 Acceptance Snapshot

- Backend: 231 passed, 1 environment skip, 83.31% coverage.
- Frontend lint, production build, credential scan, and four Rust shell tests passed.
- Agent Eval: core 18/18, Multi-Agent 3/3, professional Agent 4/4.
- False completion, permission violation, sandbox violation, and unrelated file modification counts were zero.
- Packaged sidecar median cold readiness remained below the 3-second budget.
- NSIS upgrade from the accepted v0.22 baseline preserved data, migrated schema 14 to 15, created a pre-migration backup, stopped the sidecar, and preserved application data after uninstall.
- GitHub CI and Windows Release workflows passed and uploaded NSIS, MSI, and SBOM artifacts.

## Historical Boundaries

The accepted baseline remains a personal Windows Agent restricted to a user-selected workspace. Cloud execution, multi-user access, mobile-local execution, arbitrary third-party code, unlimited child Agents, and automatic permission expansion were intentionally excluded.

For v2.0.0, web and mobile product work is frozen rather than removed. The platform-neutral React/API layers remain buildable, while release acceptance is based on the installed Windows desktop application and its packaged sidecar.

## v2.0.0 Acceptance Snapshot

- Backend: 238 passed, 1 platform skip, 83.41% coverage; frontend and Rust gates passed.
- Agent Eval: core 18/18, Multi-Agent 3/3, professional Agent 4/4, adversarial 4/4; all safety violation counters remained zero.
- Real DeepSeek credential check succeeded without exposing or persisting the key outside Windows Credential Manager.
- Packaged sidecar startup median was 2519 ms and maximum was 2698 ms across three samples.
- v1.0.0 NSIS upgrade preserved data, migrated schema 14 to 15, removed the legacy Agent executable and uninstall identity, and produced verified NSIS, Simplified Chinese MSI, and SBOM outputs.
