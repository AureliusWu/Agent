# Git Privacy Audit

## Result

The repository is private. Reachable branch and tag history was rewritten before 7.0.0 implementation to remove a runtime acceptance transcript and a user-supplied character image. Synthetic key examples and historical machine-specific paths were normalized.

## Evidence

- No SQLite blobs were found in reachable Git objects.
- The only historical blob larger than 1 MB was the removed user image.
- Secret-format scanning now reports only explicitly allowed synthetic markers.
- Tracked-content and full-history privacy scans pass.
- GitHub Actions artifacts were deleted; the remote artifact count is zero.
- No GitHub Releases were present, and no active Pages deployment was found.

## Guardrails

- `.gitignore` excludes runtime directories, databases, logs, credentials, exports, user assets, and generated outputs.
- Local-only notes and manual evidence directories are excluded via `.git/info/exclude`.
- CI and release workflows run privacy checks before tests or packaging.
- Live-model evaluation output is not uploaded as a CI artifact.

## Residual Risk

Private local backups intentionally retain pre-rewrite recovery evidence outside the repository. Remote forks, clones, caches, or third-party mirrors outside this repository's control cannot be revoked by a local history rewrite.
