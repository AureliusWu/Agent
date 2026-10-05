# v16 controlled pytest capture and checkout bytes

This document describes new evidence capture, not a release verdict. Existing
receipts and historical FAIL/BLOCKED results must never be rewritten.

## Fixed execution protocol

`scripts/rc_test_evidence.py` now captures `isolated-pytest-v2` (schema 2).
Its actual subprocess argv uses the interpreter basename and repository-relative
paths, with the fixed `siyi` working directory. The subprocess `executable`
parameter selects the exact current interpreter independently of PATH. The
receipt records its basename, CPython version and executable SHA-256, not its
private installation directory.

The command still executes **all** `tests/backend`, loads only the named pytest
plugins, clears inherited selection/configuration variables, and enforces 80%
application coverage. Host `AGENT_*` and `SIYI_*` settings are removed and replaced
with fresh synthetic data, an empty acceptance environment file and mock/no-paid
provider settings. This does not isolate the Windows Credential Manager under
the same user account and must not be treated as a credential sandbox.

Coverage filenames are captured relative to `siyi` (`app/...`) using
`relative_files = true`. Absolute, escaping and non-application coverage paths
are rejected for new v2 receipts both at capture and in `rc_gate.py`.

Source binding, exact attachment hashes, full pre-selection collection, raw
JUnit/coverage arithmetic, all four native recovery probes and mandatory
specialized no-skip checks remain mandatory. A zero pytest exit code alone is
not a PASS; unavailable Windows symlink privileges still block the strict gate.

The historical `isolated-pytest-v1` absolute-path protocol remains readable only
against its original source/output paths. It is not made portable by editing
its argv, raw attachments or hashes.

## Capture

From an appropriate isolated Windows test process, use the repository's locked
Python 3.12 environment and a **fresh** output directory:

```powershell
siyi/.venv/Scripts/python scripts/record-rc-check.py --gate python_full_tests --gate coverage_80 --output accepted/backend-unique-run.json
```

Selecting only backend requirements executes the fixed complete v2 pytest
command directly. The outer envelope records that actual command and `siyi`
working directory, exact raw-result hashes, and source identity before/after.
Each additional specialized matrix requirement must be explicitly selected with
its own `--gate`; admitting any direct backend check still independently validates
all original mandatory backend raw checks. Frontend, Rust, model and performance
requirements cannot be granted by this path. Mixed full-stack selection retains
the actual `scripts/test.ps1` entry point.

`rc_test_evidence.py` remains the raw runner used by CI and by the collector. Its
execution receipt alone does not add a source envelope or prove other stacks.
Do not wrap an old direct run in a fictitious `scripts/test.ps1` command or bind
it after the fact to changed source. Historical full-stack plus inner v2 receipts
remain readable without changing their original commands.

Keep raw failures locally. The accepted artifact exporter only exports the exact
hashed attachment closure after all original RC gates pass; synthetic data,
databases, environment files, private logs and test temporary directories are
not publication inputs. These tests must not execute installers, microphones,
paid provider calls or the real user's application data.

## Checkout contract

Source identity v2 hashes actual file bytes, not a normalized text representation.
`.gitattributes` therefore fixes text checkout line endings to LF and marks common
binary assets as non-text. Do not change the identity algorithm, pretend an old
CRLF/mixed build matches a new LF build, or rewrite historical fingerprints.

After a source milestone is committed, build and capture against one clean,
canonical checkout. Verify before/after source identity and preserve the locked
manifest. A GitHub acceptance workflow must check out that same commit and obey
the committed attributes. A newly added line-ending contract does not qualify
any earlier candidate or receipt.

## Remaining gates

Protocol unit tests prove transport/control behavior only. They do not replace a
clean full backend run, actual desktop startup, default-model qualification,
installed NSIS/MSI upgrade/retention/uninstall, human interaction/voice acceptance,
or a real successful artifact upload/download and release workflow.

## Runtime scheduling follow-up — 2026-10-05

GitHub CI run `37132605789` on clean `ba5b547` failed two task-runtime
completion checks. That failed run remains retained; later local checks do not
change its conclusion or qualify a newly built candidate.

The test module previously shared the session SQLite database. Application
startup correctly recovered earlier pending tasks, but those tasks entered the
current test's mocked provider. A generic model-finished event could therefore
refer to another task. A retained seeded diagnostic reproduced the foreign
prompts; function-owned SQLite plus exact task binding removed that interference.
`test_task_runtime.py` now owns one database per test function and preserves that
same database across restarts within the function. The provider completion
signal and first-task gate bind the actual task ID. The original five-second
submission limit, ten-second completion checks, four-second queue checks and
terminal-state assertions remain unchanged.

The diagnostic also exposed a separate product bug: queue items with equal
priority and creation timestamps were ordered by random UUID. Both pending-item
selection and steering consumption now use SQLite insertion order (`rowid`)
for that final tie, without changing priority, timestamps, claims, permissions
or schema. The identifier remains the UUID; `rowid` is not a permanent public
identity. This narrowly covers the current ordinary SQLite table and actual
SQLite backup/restore path, not arbitrary table reconstruction or JSON imports.
VACUUM may renumber rowids; tests verify relative order through the actual
backup privacy-scrub/VACUUM and restore path rather than assuming fixed values.

Actual development checks, with all raw failures preserved privately:

- Database/mock isolation: 27 related cases passed; 12 seeded-isolation cases
  passed, with no foreign prompt entering either target mock.
- Frozen timestamp and reverse-UUID queue regression: all three new cases
  failed against the original product ordering. All three passed after the fix.
- Queue/lease focused check: 19 passed, zero failures or skips.
- Final three-module combination (`test_runtime_v3.py`, `test_task_leases.py`,
  `test_task_runtime.py`): 30 passed, zero failures or skips, exit zero. Source
  before/after contained exactly the same three uncommitted source/test changes.

These are focused development checks, not a fresh complete backend/80% coverage
capture, successful remote CI or formal RC acceptance. The earlier isolated
24-pass/one-FIFO-failure result and an intermediate new-test connection-lock
failure remain retained. The Starlette TestClient deprecation warning remains;
no dependency was upgraded to suppress it.

The independently checked `ba5b547` frozen portable candidate and its actual
NSIS/MSI bundles remain useful historical evidence for that exact source only.
Bundle status is `BUNDLED_NOT_ACCEPTED`, not installation or release acceptance.
Later scheduling changes require a new clean source capture and new candidate
identity; do not relabel those binaries or substitute their successful startup
for UI, microphone, real-model, installed lifecycle or performance acceptance.
