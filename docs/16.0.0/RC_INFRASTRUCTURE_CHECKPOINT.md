# v16 acceptance infrastructure checkpoint — 2026-10-03

**Status: implementation checkpoint, not RC acceptance and not a release.**
Product source metadata remains `16.0.0`; the root portable application has not
been replaced. Historical failed/blocked gates and candidate identities remain
unchanged. No v16 tag, formal release, real installer run, microphone capture,
paid call or model download was performed for this checkpoint.

## Implemented and checked

- The desktop collector now launches suspended processes into an exact
  `KILL_ON_JOB_CLOSE` Job before resume, retains native handles, verifies actual
  process identity/membership, and closes only the owned main window. Unknown
  accounting, forced termination, cleanup faults or outstanding handles cannot
  become a successful shutdown. Retained data/launch markers require proven
  prior cleanup; failure evidence is not deleted.
- The explicit installed-lifecycle helper implements eleven ordered stages,
  with durable intent before effects, exact artifacts, owned fixtures,
  registration/shortcut collision checks and failure retention. Its default
  entry plans only; `--execute` does not request elevation or approve unknown
  package behavior. Its tests are synthetic, **not installed-app evidence**.
- The artifact exporter/receiver checks exact reference closure, hashes, ZIP
  safety, required files, private content and immutable source identity. The
  new main-only acceptance workflow independently reruns the existing importer,
  installer validator and original total RC gates before success-only upload.
  No actual transport asset or workflow run has been created.
- Installed capture now writes private argv/logs unchanged before separately
  creating the exact-schema `installed-public-v1` receipt. Actual current render
  observations and the complete fixture-scoped inventory remain bound to the
  candidate bytes; receive rechecks typed semantics and ordinary reference
  closure, even against a rebuilt incoming index. Synthetic fixtures cannot
  acquire actual-run or RC qualification.
- Original official legacy packages can use their observed adjacent/embedded
  identities without a fabricated current-format build audit. Missing legacy
  fields remain unknown. Stock MSI actions were inspected read-only and are
  allowlisted by their exact definitions, conditions and ordering; MSI lifecycle
  holds native handles and an exclusively owned sentinel to protect empty real
  Desktop directories. Native guard tests use only pytest-owned directories.
  NSIS package provenance is not a claim of whole-program static review, and the
  actual old NSIS publisher field remains unknown.
- Fixed-name read-only host probes now use a narrow host-folder environment and
  only the trusted native PowerShell module directory. Product and installer
  dispatch keep their original isolated environment. Actual host observation
  passed without installer/application mutation; it found the existing root-v15
  shortcut, which remains untouched. Empty or unknown known folders still block.
- New pytest captures use a fixed relative-path v2 protocol, exact interpreter
  binding, complete backend collection, 80% application coverage and unchanged
  mandatory native/specialized no-skip checks. Old v1 receipts are not rewritten.
- Text checkout bytes are fixed to LF by `.gitattributes`. A mechanical LF-only
  formatting pass normalized 393 tracked text files; the 917 tracked text files
  now have LF checkout bytes. Six binary files remained non-text. This did not
  introduce semantic edits to the other tracked files.

## Actual checkpoint validation

The earlier integrated release-domain pytest run completed **339 passed, zero
skipped**, using the existing backend conftest and isolated synthetic fixtures.
This includes native Windows Job probes with harmless standard-library parent
and child processes, not the actual product. Raw JUnit is retained privately.

After the CI portability and installed-public transport corrections, an
integrated development check of the entire release test directory plus the
three affected infrastructure files completed **575 passed, zero failures,
zero skipped**, with pytest exit code zero. The `audioop` Python 3.13 deprecation
warning remains. After the fixture/path, real uppercase fingerprint and
host-only observation follow-ups, the same integrated target was rerun:
**590 passed, zero failures, zero skipped; pytest exit code zero**. These
development results do not replace a new complete backend/coverage capture on
the final clean source.

Tracked source/history privacy checks and all added-source privacy checks passed.
`scripts/check-release-metadata.py` passed all **15** version sources.
`git diff --check` passed. Earlier focused runs and failures remain retained and
are not rebound to the final source fingerprint.

An elevated, test-only complete backend capture subsequently passed on clean
commit `e9e51f173c3d9c9b7c84d38619b91487b7667948`, with identical source before and
after (fingerprint `44B52D35682DFAFFA4E9BFED68CB065E479E26E6B58798CD84EC496ABEAF8411`).
The actual collection contained **2,329 cases: 2,306 passed, 23 explicitly
unselected live-service cases skipped, zero failures**. Application coverage was
**83.90536851683349%**. All **44 sandbox cases** and all **four Windows native
recovery probes** executed and passed without skips; all ten raw backend
validators passed. The helper did not change system policy or execute installers,
microphone operations, paid models or downloads.

Raw results remain under
`build/v1600-evidence/accepted/backend-c7e42a8a98094c618897bc6ca1b17952/`.
Execution SHA-256 is
`199513B4993E0869EAF1B671D772C3A0EA0CF45E873C214669DE5AA872B09C24`;
JUnit SHA-256 is
`98AD57F285525EF67A7B9F7AE60BC234A825D5FFB88E612AF762803CBB5FBD12`.
This is genuine backend evidence for that exact source, not a current candidate,
installed-app, manual, local-model or formal release proof. Later source changes
do not rebind this capture. Standalone controlled-pytest capture must also have a
truthful admitted command, rather than claiming that `scripts/test.ps1` ran.

The corresponding GitHub CI run `37100169943` failed with five backend failures
(2,299 passed, 25 skipped; coverage 83.85%). Three fixtures assumed the local
desktop `.venv` existed; two assumed their GBK byte fixture was also the hosted
Windows ANSI code page. These failures remain recorded. The fixtures now select
their intended interpreter/code page explicitly, while missing-runtime negative
checks remain fail-closed. The actual Windows identity reader emits explicit
UTF-8 bytes and rejects lossy, replacement or malformed text, independently of
host locale. Focused checks passed; a read-only real self-process identity probe
also passed. This is not a claim that the next remote CI run has passed.

## Unclosed production boundaries

1. The new dual-stream protocol and typed inventory closure have synthetic
   integration coverage, not a successful actual installed capture. Produce new
   receipts during the real lifecycle; do not alter old raw commands or rebind
   historical runs. See `RC_ARTIFACT_PRODUCTION.md`.
2. Original official previous-package bytes and read-only stock package metadata
   were inspected. The optional `no-bootstrap-build-v1` audit is not an original
   v16 gate and does not require rebuilding a historical official package.
   Package metadata/provenance and native guard probes do not replace actual
   installation, startup, upgrade, preservation, cleanup and uninstall evidence.
3. A Windows Installer service can perform effects outside the client Job.
   Client termination is not proof of whole-system rollback or server ownership;
   uncertain effects require retained operator recovery, not automatic retry.
4. The isolated elevated backend/symlink run above resolved the prior token
   blocker for its source. No global Developer Mode, privilege policy or security
   setting was changed. A future frozen-source run must retain these same native
   and specialized no-skip checks.
5. Preserve the original performance thresholds and all old failures. New
   paired same-host measurements, default-model/core live evaluation, P01 file
   qualification and actual desktop startup still require new evidence.
6. Installed NSIS/MSI lifecycle, human UI/file/permission/provider/MCP/memory/
   voice acceptance, exact artifact upload/download, formal workflow and final
   portable replacement are still separate required gates.

The implementation plan and original total RC contracts remain authoritative.
This checkpoint does not narrow the v16 completion objective.
