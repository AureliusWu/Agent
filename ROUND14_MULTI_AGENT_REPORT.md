# Round 14: Controlled Multi-Agent Acceptance Report

## Release

- Version: `0.12.0`
- Database schema: `11`
- Scope: controlled planner-executor, generator-verifier, and parallel-explorer orchestration
- Default: single Agent; multi-Agent modes are explicit and bounded

## Implemented Boundaries

- Child Agents are read-only, depth-one workers with explicit roles, file scopes, tool allowlists, token budgets, timeouts, and cancellation propagation.
- Child Agents cannot call MCP, run commands, write files, change permissions, or create more children.
- Only the root Agent may write. Database-backed file locks record before/after hashes and stop on conflicts; no automatic merge is attempted.
- Child output is treated as untrusted input, checked for prompt injection, redacted before reuse, and recorded in the data-flow audit.
- Parent/child runs, costs, file locks, verification, and tool activity are visible in the audit Trace.

## Verification

| Gate | Result |
| --- | --- |
| Backend tests | `156 passed, 1 skipped`; coverage `84.07%` |
| Frontend lint/build | Passed; JS `352.48 kB` (`109.59 kB` gzip), CSS `21.48 kB` (`4.88 kB` gzip) |
| Rust tests/build | `2/2` passed; release build and both Windows bundles succeeded |
| Multi-Agent eval | Four consecutive `3/3` runs; final average `250 ms`, `372` tokens |
| Core eval | `18/18`; no false success, permission violation, sandbox violation, or unrelated file change |
| Release gate | Passed against the Round 13 baseline |
| Responsive UI | Passed at `1440x900` and `390x844`; no horizontal overflow or control overlap |
| Packaged sidecar | Health `ok`, version `0.12.0`, schema `11`, multi-Agent enabled |

The three stable optimized core samples had a median task time of `509 ms` versus the Round 13 baseline of `464 ms` (`+9.7%`), within the `15%` project regression budget. The final sample was `519 ms`; variance remains below the release gate.

## Artifacts

- MSI: `frontend/src-tauri/target/release/bundle/msi/Agent_0.12.0_x64_en-US.msi` (`31,150,080` bytes)
- NSIS: `frontend/src-tauri/target/release/bundle/nsis/Agent_0.12.0_x64-setup.exe` (`30,015,631` bytes)

## Remaining Limits

- This release intentionally does not support arbitrary nesting, child writes, automatic conflict merging, or multi-Agent MCP execution.
- File locks coordinate Agent-owned writes; external editors remain protected by hash conflict detection rather than OS-level locks.
- The next round is professional Agent profiles and a permission-aware extension SDK.
