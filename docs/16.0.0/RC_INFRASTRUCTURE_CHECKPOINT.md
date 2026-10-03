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
- New pytest captures use a fixed relative-path v2 protocol, exact interpreter
  binding, complete backend collection, 80% application coverage and unchanged
  mandatory native/specialized no-skip checks. Old v1 receipts are not rewritten.
- Text checkout bytes are fixed to LF by `.gitattributes`. A mechanical LF-only
  formatting pass normalized 393 tracked text files; the 917 tracked text files
  now have LF checkout bytes. Six binary files remained non-text. This did not
  introduce semantic edits to the other tracked files.

## Actual checkpoint validation

The final integrated release-domain pytest run completed **338 passed, zero
skipped**, using the existing backend conftest and isolated synthetic fixtures.
This includes native Windows Job probes with harmless standard-library parent
and child processes, not the actual product. Raw JUnit is retained privately.

Tracked source/history privacy checks and all added-source privacy checks passed.
`scripts/check-release-metadata.py` passed all **15** version sources.
`git diff --check` passed. Earlier focused runs and failures remain retained and
are not rebound to the final source fingerprint.

These results do **not** claim the full backend RC gate, clean candidate build,
actual desktop/installer/manual acceptance, local-model qualification or formal
uploaded release. Subsequent capture must bind a frozen clean source identity.

## Unclosed production boundaries

1. The installer helper's private actual argv and fixture-scoped inventory are
   not yet compatible with public transport. Define a new safe dual-stream
   capture protocol and typed inventory scope, test full consumption/import,
   and recapture. Do not alter old raw commands, drop ordinary references or
   weaken privacy checks. See `RC_ARTIFACT_PRODUCTION.md`.
2. Actual no-bootstrap build-audit production and original official previous
   package provenance remain absent. Stock MSI authored actions require explicit
   review. Source/template hashes or a self-filled boolean cannot approve bytes.
3. A Windows Installer service can perform effects outside the client Job.
   Client termination is not proof of whole-system rollback or server ownership;
   uncertain effects require retained operator recovery, not automatic retry.
4. Complete backend/symlink qualification needs the appropriate isolated Windows
   token. An elevated test-only helper is prepared locally; no global Developer
   Mode, privilege policy or security setting is changed.
5. Preserve the original performance thresholds and all old failures. New
   paired same-host measurements, default-model/core live evaluation, P01 file
   qualification and actual desktop startup still require new evidence.
6. Installed NSIS/MSI lifecycle, human UI/file/permission/provider/MCP/memory/
   voice acceptance, exact artifact upload/download, formal workflow and final
   portable replacement are still separate required gates.

The implementation plan and original total RC contracts remain authoritative.
This checkpoint does not narrow the v16 completion objective.
