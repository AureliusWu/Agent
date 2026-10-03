# Git Privacy Audit

## Result

This is the historical 7.0.0 audit, not a statement of current GitHub visibility.
The repository was private at that audit and is public as verified on 2026-10-03.
Reachable branch and tag history was rewritten before 7.0.0 implementation to remove a runtime acceptance transcript and a user-supplied character image. Synthetic key examples and historical machine-specific paths were normalized.

## Historical evidence (7.0.0)

- No SQLite blobs were found in reachable Git objects.
- The only historical blob larger than 1 MB was the removed user image.
- Secret-format scanning now reports only explicitly allowed synthetic markers.
- Tracked-content and full-history privacy scans pass.
- GitHub Actions artifacts were deleted; the remote artifact count is zero.
- No GitHub Releases were present, and no active Pages deployment was found.

## Current synchronization scope (2026-10-03)

The current GitHub repository is public and has three historical Releases
(`v0.22.0`, `v7.0.0`, `v8.0.1`). Actions artifact count is currently zero.
The earlier “no Releases” statement is historical and must not be used as a
current guarantee. Existing release installers have not been inspected for all
possible private embedded content in this synchronization task.

Current local and remote main source, plus the merged public working tree, were
checked with the repository privacy scanner. Unredacted local acceptance
documents, runtime data, logs, and recovery bundles are stored outside the public
checkout. Current source version remains a **16.0.0 development candidate**;
Git synchronization is not a formal product release. See
[GITHUB_SYNC_2026-10-03.md](../GITHUB_SYNC_2026-10-03.md) for scope and limitations.

## Guardrails

- `.gitignore` excludes runtime directories, databases, logs, credentials, exports, user assets, and generated outputs.
- Local-only notes and manual evidence directories are excluded via `.git/info/exclude`.
- CI and release workflows run privacy checks before tests or packaging.
- Live-model evaluation output is not uploaded as a CI artifact.

## Residual Risk

Private local backups intentionally retain pre-rewrite recovery evidence outside the repository. Remote forks, clones, caches, or third-party mirrors outside this repository's control cannot be revoked by a local history rewrite.
