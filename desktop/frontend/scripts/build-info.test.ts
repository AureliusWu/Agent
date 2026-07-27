import assert from 'node:assert/strict'
import { evaluateBuildConsistency } from '../src/buildInfoModel.ts'
import type { BuildManifest } from '../src/buildInfoModel.ts'

function manifest(buildId: string): BuildManifest {
  return {
    manifest_version: 1, product_version: '2.0.1', git_commit: 'a'.repeat(40), git_short_commit: 'a'.repeat(12),
    git_branch: 'test', build_time: '2026-07-16T00:00:00Z', build_type: 'Release', workspace_state: 'CLEAN',
    source_fingerprint: 'b'.repeat(64), build_id: buildId,
    component_build_ids: { tauri: `tauri-${buildId}`, react: `react-${buildId}`, sidecar: `sidecar-${buildId}` },
    database_schema_version: 22,
    release_status: {
      target_version: '2.0.2', source_version: '2.0.1', implementation_status: 'PARTIAL',
      test_status: 'NOT_READY', distribution_status: 'NOT_DISTRIBUTED',
    },
    evidence_manifest_hash: 'c'.repeat(64),
  }
}

const current = manifest('current')
assert.equal(evaluateBuildConsistency(current, current, current).status, 'consistent')
assert.equal(evaluateBuildConsistency(current, current, manifest('old-sidecar')).status, 'mismatch')
assert.deepEqual(evaluateBuildConsistency(current, null, current).missing, ['desktop'])
console.log('Build consistency tests passed: consistent, old-sidecar mismatch, incomplete')
