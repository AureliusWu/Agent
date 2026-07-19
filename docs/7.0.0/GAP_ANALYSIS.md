# 7.0.0 Gap Analysis

## P0 Correctness

- File locks identify an agent rather than the owning task and do not renew leases.
- Tool receipts do not fully distinguish reads from mutations or prove before/after state.
- Mutation APIs do not consistently require an expected version token.
- Long-term administrator confirmation is represented by a boolean instead of an auditable grant.
- Task ownership lacks a complete lease/heartbeat/takeover protocol.
- Child processes are not governed by one durable managed-process supervisor.
- Provider failures do not distinguish transient throttling, exhausted quota, and invalid credentials.
- Timezone handling contains a fixed regional assumption.

## Context And UX

- Context assembly is structured in parts but lacks a single typed Context Compiler v2 contract and complete source manifest.
- Automatic first-turn conversation titles and manual title locking are incomplete.
- Source modules remain distributed under historical top-level names instead of the target ownership boundaries.

## Release Gaps

- Schema 26 migration and rollback proof are pending.
- Long-task, lock-conflict, crash-recovery, quota, context-pressure, and soak tests are pending.
- Clean-machine upgrade and formal desktop package smoke tests are pending.
- Required 7.0.0 acceptance reports and final privacy evidence are pending.
