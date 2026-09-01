import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  canManageMemoryHere,
  memoryCategoryLabel,
  memoryOwnerHint,
} from '../src/shared/memoryOwnership.ts'

const longTermCompatibility = {
  owner_api: 'long_term' as const,
  editable_via_current_api: false,
}
const scopedCompatibility = {
  owner_api: 'scoped' as const,
  editable_via_current_api: false,
}

assert.equal(canManageMemoryHere({ owner_api: 'scoped', editable_via_current_api: true }, 'scoped'), true)
assert.equal(canManageMemoryHere({ owner_api: 'long_term', editable_via_current_api: true }, 'long_term'), true)
assert.equal(canManageMemoryHere({ owner_api: 'long_term', editable_via_current_api: true }, 'scoped'), false)
assert.equal(canManageMemoryHere(longTermCompatibility, 'scoped'), false)
assert.equal(canManageMemoryHere(scopedCompatibility, 'long_term'), false)
assert.equal(canManageMemoryHere({}, 'scoped'), false, 'missing ownership metadata must fail closed')
assert.equal(memoryCategoryLabel(longTermCompatibility), '长期记忆')
assert.equal(memoryCategoryLabel({ owner_api: 'scoped' }, '技术决策'), '技术决策')
assert.equal(memoryOwnerHint(longTermCompatibility), '请在“长期记忆”中管理')
assert.equal(memoryOwnerHint(scopedCompatibility), '请在“我的记忆”中管理')

const scopedManager = readFileSync(new URL('../src/components/memory/MemoryManager.tsx', import.meta.url), 'utf8')
const longTermManager = readFileSync(new URL('../src/components/memory/LongTermMemoryManager.tsx', import.meta.url), 'utf8')
assert.match(scopedManager, /canManageMemoryHere\(item, 'scoped'\)/)
assert.match(scopedManager, /memoryCategoryLabel\(item,/)
assert.match(scopedManager, /memoryOwnerHint\(item\)/)
assert.match(longTermManager, /canManageMemoryHere\(item, 'long_term'\)/)
assert.match(longTermManager, /memoryOwnerHint\(item\)/)

console.log('Memory ownership contract tests passed')
