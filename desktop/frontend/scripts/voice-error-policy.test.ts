import assert from 'node:assert/strict'
import { preserveVoiceFailure, userFacingVoiceError, voiceTranscriptionFailureDisposition } from '../src/voiceErrorPolicy.ts'

const ramError = Object.assign(new Error('available RAM 617377792 bytes is below the safe threshold 2147483648 bytes'), {
  detail: { code: 'RESOURCE_RAM_PRESSURE', resource: { kind: 'ram', available_bytes: 617377792, minimum_available_bytes: 2147483648 } },
})
const message = userFacingVoiceError(ramError)
assert.match(message, /可用内存不足/)
assert.match(message, /0\.57 GiB/)
assert.match(message, /2\.00 GiB/)
assert.match(message, /重新录音/)
assert.match(message, /无需重启司忆/)
assert.match(message, /文字输入/)
assert.doesNotMatch(message, /available RAM|617377792|2147483648/)

// Older Sidecars and metadata-only SSE events have a code but no byte counts.
for (const error of [{ detail: { code: 'RESOURCE_RAM_PRESSURE' } }, { code: 'RESOURCE_RAM_PRESSURE' }]) {
  const fallback = userFacingVoiceError(error)
  assert.match(fallback, /可用内存不足/)
  assert.doesNotMatch(fallback, /GiB|NaN|undefined|0\.00/)
}
for (const value of [null, undefined, -1, NaN, Infinity, true, '617377792', 0.5, Number.MAX_SAFE_INTEGER + 1]) {
  const invalid = userFacingVoiceError({ detail: { code: 'RESOURCE_RAM_PRESSURE', resource: { kind: 'ram', available_bytes: value, minimum_available_bytes: 2147483648 } } })
  assert.doesNotMatch(invalid, /GiB|NaN|undefined/)
}
assert.match(userFacingVoiceError({ code: 'RESOURCE_RAM_PRESSURE', resource: { kind: 'ram', available_bytes: 0, minimum_available_bytes: 2147483648 } }), /0\.00 GiB/)
assert.doesNotMatch(userFacingVoiceError({ code: 'RESOURCE_RAM_PRESSURE', resource: { kind: 'vram', available_bytes: 0, minimum_available_bytes: 123 } }), /GiB/)
const vramMessage = userFacingVoiceError({ code: 'RESOURCE_VRAM_PRESSURE', resource: { kind: 'vram', available_bytes: 268435456, minimum_available_bytes: 536870912 } })
assert.match(vramMessage, /显存不足.*0\.25 GiB.*0\.50 GiB/)
assert.match(vramMessage, /切换到 CPU/)
assert.equal(userFacingVoiceError(new Error('本地核心已重启')), '本地核心已重启')
assert.match(userFacingVoiceError(new DOMException('denied', 'NotAllowedError')), /麦克风权限被拒绝/)
assert.doesNotMatch(userFacingVoiceError({ code: 'STT_PERMISSION_DENIED' }), /麦克风权限被拒绝/)
assert.match(userFacingVoiceError(new DOMException('cancelled', 'AbortError')), /已取消/)
assert.match(userFacingVoiceError(null), /失败.*重试/)

// Resource failures stay errors whether HTTP or terminal SSE arrives first.
assert.equal(voiceTranscriptionFailureDisposition(ramError, false, false), 'error')
assert.equal(voiceTranscriptionFailureDisposition(ramError, true, true), 'error')
assert.equal(voiceTranscriptionFailureDisposition(new Error('network error'), true, true), 'preserve_failure')
assert.equal(voiceTranscriptionFailureDisposition(new DOMException('cancel', 'AbortError'), true, true), 'preserve_failure')
assert.equal(preserveVoiceFailure(message), message)
assert.match(preserveVoiceFailure(''), /语音输入未完成/)
assert.equal(voiceTranscriptionFailureDisposition(ramError, true, false), 'cancelled', 'an explicit user cancellation still wins')
for (const code of ['STT_ALREADY_CANCELLED', 'STT_CANCELLED', 'VOICE_SESSION_CANCELLED']) {
  assert.equal(voiceTranscriptionFailureDisposition({ detail: { code } }, false, false), 'cancelled')
}

console.log('Voice resource error policy tests passed')
