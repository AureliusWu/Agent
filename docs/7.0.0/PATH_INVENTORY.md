# Path Inventory

## Canonical Runtime Layout

Production root: local application data under `AureliusWu/Agent/`.

Development root: local application data under `AureliusWu/Agent-Dev/`.

Both use the same layout:

```text
data/agent.db
logs/
backups/
user-assets/
artifacts/
workspaces/
cache/
crash/
state/
temp/
extensions/
kokoro/
```

## Resolution Rules

1. `AGENT_DATA_ROOT` is an explicit isolation override for tests and diagnostics.
2. Packaged Tauri sets the production environment and root.
3. Standalone development defaults to `Agent-Dev`.
4. Tests always provide an isolated temporary database.
5. No default path may resolve inside the source checkout.

## Legacy Migration

The pre-7.0 production database at the runtime root is copied to a timestamped backup, hashed, migrated with the SQLite backup API, integrity-checked, and then moved to `data/agent.db`. Source-tree runtime data follows the same backup-first principle before removal from the checkout.
