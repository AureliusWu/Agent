# Round 15: Professional Agents and Extension SDK Acceptance Report

## Release

- Version: `0.13.0`
- Database schema: `12`
- Scope: professional Agent profiles, declarative extensions, lifecycle management, and packaged Sidecar verification
- Compatibility: existing conversations, permissions, tools, Skills, MCP, audit, Trace, and verifier behavior remain available

## Implemented Boundaries

- Built-in profiles cover general, coding, data, document, and file-organization work with explicit prompts, tool scopes, Skill tags, completion standards, verifiers, and default permissions.
- Conversations persist the selected profile. Tasks freeze a profile snapshot so resume cannot silently adopt changed extension instructions.
- Declarative extension packages may contribute profile, Skill, and built-in-tool aliases without executing arbitrary package code.
- Extension tools retain the normal permission, workspace sandbox, approval, audit, Trace, and verification path. Critical delegates, reduced risk declarations, embedded credentials, unsafe paths, dependency cycles, and prompt-injection tool descriptions are rejected.
- Install, enable, disable, integrity isolation, and one-way rollback are supported. A failed or tampered extension is isolated from the main application.
- Extension-defined prompts are marked untrusted; tainted side effects require approval even when the active mode is more permissive.

## Verification

| Gate | Result |
| --- | --- |
| Backend tests | `175 passed, 1 skipped`; coverage `84.23%` |
| Frontend lint/build | Passed; JS `356.30 kB` (`110.49 kB` gzip), CSS `22.01 kB` (`4.98 kB` gzip) |
| Rust checks/tests | Format and compile passed; `2/2` tests passed |
| Professional Agent eval | Three consecutive `4/4` runs; final averages `190 ms`, `195 ms`, and `188 ms` |
| Core eval | `18/18`; average `482 ms`, `2544` tokens, and release gate passed |
| Multi-Agent eval | `3/3`; average `244 ms`, `372` tokens |
| Safety assertions | No false success, permission violation, sandbox violation, or unrelated file change |
| Responsive UI | Passed at `1440x900` and `390x844`; no horizontal overflow or bottom-navigation overlap |
| Packaged sidecar | Health `ok`, version `0.13.0`, schema `12`, five built-in profiles, multi-Agent enabled |

Compared with Round 14, the final core sample improved from `519 ms` to `482 ms` and the multi-Agent sample from `250 ms` to `244 ms`. JavaScript gzip size increased by `0.90 kB` (`0.8%`).

## Release Artifacts

- MSI: `frontend/src-tauri/target/release/bundle/msi/Agent_0.13.0_x64_en-US.msi` (`31,182,848` bytes)
- NSIS: `frontend/src-tauri/target/release/bundle/nsis/Agent_0.13.0_x64-setup.exe` (`30,047,465` bytes)
- The desktop build now runs an isolated packaged-Sidecar smoke test and cleans the spawned process tree before reporting success.

## Remaining Limits

- Extensions are intentionally declarative: no arbitrary Python, JavaScript, native code, UI injection, or unrestricted lifecycle hooks.
- SHA-256 verifies package integrity, not publisher identity. A trusted signing and distribution system is not implemented.
- The next round stabilizes kernel interfaces and adds adapter contract tests before any plugin marketplace work.
