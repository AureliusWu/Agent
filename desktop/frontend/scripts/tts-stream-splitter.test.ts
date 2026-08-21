import assert from 'node:assert/strict'
import {
  splitStreaming,
  TTS_SENTENCE_MAX_CHARS,
  type StreamBuffer,
} from '../src/ttsStreamSplitter.ts'

const buffer: StreamBuffer = { text: '', inCodeFence: false, scan: 0 }
const chunks = splitStreaming(buffer, '长'.repeat(TTS_SENTENCE_MAX_CHARS * 2 + 17))
const remaining = buffer.text.trim()

assert.deepEqual(chunks.map(value => value.length), [160, 160])
assert.equal(remaining.length, 17)
assert.equal([...chunks, remaining].join(''), '长'.repeat(337))
assert.ok(chunks.every(value => value.length <= TTS_SENTENCE_MAX_CHARS))

console.log('TTS streaming splitter tests passed')
