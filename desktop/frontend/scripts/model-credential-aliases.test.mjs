import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { modelCredentialAlias } from '../src/shared/modelCredentials.ts'

// Shared fixed vectors exercise the actual renderer implementation, without
// invoking Tauri, reading the Windows vault or connecting to any endpoint.
const vectors = JSON.parse(readFileSync(new URL('../../../contracts/model_credential_alias_v1.json', import.meta.url), 'utf8'))
for (const vector of vectors) {
  assert.equal(await modelCredentialAlias(vector.configuration), vector.alias, vector.id)
  assert.match(vector.alias, /^model_api_key\.[a-f0-9]{64}$/)
  assert.ok(vector.alias.length <= 80, vector.id)
}
console.log(`Model credential alias contract: ${vectors.length} fixed vectors passed`)
