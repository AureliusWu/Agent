import assert from 'node:assert/strict'
import { encodeMonoPcmWav, VOICE_BITS_PER_SAMPLE, VOICE_CHANNELS, VOICE_SAMPLE_RATE } from '../src/voiceAudio.ts'

function ascii(view: DataView, offset: number, length: number) {
  return Array.from({ length }, (_, index) => String.fromCharCode(view.getUint8(offset + index))).join('')
}

const samples = new Float32Array([-1, -0.5, 0, 0.5, 1])
const wav = encodeMonoPcmWav(samples)
const view = new DataView(await wav.arrayBuffer())

assert.equal(wav.type, 'audio/wav')
assert.equal(wav.size, 44 + samples.length * 2)
assert.equal(ascii(view, 0, 4), 'RIFF')
assert.equal(ascii(view, 8, 4), 'WAVE')
assert.equal(ascii(view, 12, 4), 'fmt ')
assert.equal(view.getUint16(20, true), 1)
assert.equal(view.getUint16(22, true), VOICE_CHANNELS)
assert.equal(view.getUint32(24, true), VOICE_SAMPLE_RATE)
assert.equal(view.getUint16(34, true), VOICE_BITS_PER_SAMPLE)
assert.equal(ascii(view, 36, 4), 'data')
assert.equal(view.getUint32(40, true), samples.length * 2)
assert.equal(view.getInt16(44, true), -32768)
assert.equal(view.getInt16(52, true), 32767)

console.log('Voice WAV format tests passed')
