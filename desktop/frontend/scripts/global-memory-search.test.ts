import assert from 'node:assert/strict'
import { groupGlobalMemoryResults } from '../src/shared/globalMemorySearch.ts'
import type { GlobalMemorySearchItem } from '../src/types.ts'

const base = {
  id: 1,
  key: 'preference.language',
  content: '使用中文',
  kind: 'project',
  namespace: 'personal',
  category: 'decision',
  source: 'user',
  tags: [],
  applicable_version: null,
  confidence: 0.9,
  effective_confidence: 0.9,
  use_count: 0,
  success_count: 0,
  failure_count: 0,
  rejected: 0,
  status: 'active',
  stale_reasons: [],
  created_at: '2026-08-01T00:00:00+00:00',
  updated_at: '2026-08-01T00:00:00+00:00',
  matched_terms: ['中文'],
  score: 1,
} satisfies Omit<GlobalMemorySearchItem, 'search_scope'>

const grouped = groupGlobalMemoryResults([
  { ...base, search_scope: 'global' },
  { ...base, id: 2, namespace: 'project', search_scope: 'project' },
])
assert.deepEqual(grouped.global.map(item => item.id), [1])
assert.deepEqual(grouped.project.map(item => item.id), [2])
console.log('Global memory search grouping tests passed')
