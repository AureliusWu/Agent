import assert from 'node:assert/strict'
import { highlightSegments } from '../src/shared/highlightText.ts'

assert.deepEqual(highlightSegments('alpha beta alpha', ['alpha']), [
  { text: 'alpha', matched: true },
  { text: ' beta ', matched: false },
  { text: 'alpha', matched: true },
])
assert.deepEqual(highlightSegments('没有匹配', []), [{ text: '没有匹配', matched: false }])

console.log('Search highlight tests passed')
