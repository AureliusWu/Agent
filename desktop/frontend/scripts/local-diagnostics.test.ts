import assert from 'node:assert/strict'
import { readLocalDiagnostic, resourceBytes, resourceSampleLabel } from '../src/localDiagnosticPolicy.ts'

const [ollama, stt] = await Promise.all([
  readLocalDiagnostic('Ollama', async () => { throw new Error('secret endpoint details') }),
  readLocalDiagnostic('STT', async () => ({ status: 'READY' })),
])
assert.equal(ollama.value, null)
assert.match(ollama.error!, /状态未知/)
assert.doesNotMatch(ollama.error!, /secret/)
assert.equal(stt.value?.status, 'READY')
assert.equal(stt.error, null)
for (const value of [undefined, null, NaN, Infinity, -1]) assert.equal(resourceBytes(value), '未知')
assert.equal(resourceBytes(0), '0 B')
assert.equal(resourceBytes(2 * 1024 ** 3), '2.00 GiB')
assert.equal(resourceSampleLabel(undefined), '采样时间未知')
assert.match(resourceSampleLabel('2026-09-30T00:00:00Z', Date.parse('2026-09-30T00:00:31Z')), /旧采样/)
assert.match(resourceSampleLabel('2026-09-30T00:00:00Z', Date.parse('2026-09-30T00:00:01Z')), /采样于/)
console.log('V16 local diagnostics: independent failures, unknown/zero and sample age passed')
