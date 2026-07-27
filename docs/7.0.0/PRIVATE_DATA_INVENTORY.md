# Private Data Inventory

## Production Runtime

Production data is stored below the operating system's local application-data directory under `AureliusWu/Agent/`. The audit found one active SQLite database plus backups, logs, artifacts, extensions, and security snapshots. Database content was not exported or committed; only schema-safe counts and integrity metadata were inspected.

## Development Runtime

Before Stage 1, ignored source-tree locations contained:

- 9 SQLite database-related files.
- 6 log or diagnostic files.
- 210 root evaluation/log files, approximately 47 MB.
- 35 backend runtime files, approximately 11 MB.
- 5 workspace backup files.

These items were never tracked. Stage 1 migrated them to `AureliusWu/Agent-Dev/` with backup manifests and SHA-256 checksums; no legacy runtime directory remains in the checkout.

## Repository Policy

The repository must never contain real conversations, memories, databases, keys, user assets, runtime logs, crash dumps, exports, or generated model/tool output. Synthetic fixtures must be visibly fake and minimal. CI runs `scripts/privacy_scan.py` against tracked content and Git history.

## Backup Policy

History-rewrite and runtime-migration backups are private local artifacts outside the checkout. They must not be uploaded to GitHub Actions, Releases, Pages, or commits.
