import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { shouldAutoSendVoiceTranscript } from '../src/voiceAutoSendPolicy.ts'

const reliable = { text: '可以自动发送', confidence: 0.91, reliable: true, reliability_reason: 'RELIABLE' }
assert.equal(shouldAutoSendVoiceTranscript(true, reliable), true, 'explicit reliable result may auto-send')
assert.equal(shouldAutoSendVoiceTranscript(false, reliable), false, 'the user preference remains authoritative')
assert.equal(shouldAutoSendVoiceTranscript(true, { ...reliable, confidence: 0.59 }), false, 'low confidence must remain in review')
assert.equal(shouldAutoSendVoiceTranscript(true, { ...reliable, reliable: false }), false, 'provider rejection must remain in review')
assert.equal(shouldAutoSendVoiceTranscript(true, { ...reliable, confidence: null }), false, 'missing confidence must fail closed')
assert.equal(shouldAutoSendVoiceTranscript(true, { ...reliable, text: '   ' }), false, 'blank text must fail closed')

const chatPath = fileURLToPath(new URL('../src/hooks/useAgentChat.ts', import.meta.url))
const capturePath = fileURLToPath(new URL('../src/hooks/useVoiceCapture.ts', import.meta.url))
const chat = (await readFile(chatPath, 'utf8')).replace(/\r\n/g, '\n')
const capture = (await readFile(capturePath, 'utf8')).replace(/\r\n/g, '\n')

const sendStart = chat.indexOf('async function send(')
const voiceSnapshot = chat.indexOf('const voiceSessionId = existingTaskId ? null : voiceSessionRef.current?.id || null', sendStart)
const route = chat.indexOf('composerRoute(content, { existingTaskId, busy, voiceSessionId })', sendStart)
const taskPost = chat.indexOf("await api<TaskSnapshot>('/api/tasks'", sendStart)
assert.ok(sendStart >= 0 && voiceSnapshot > sendStart && route > voiceSnapshot, 'send must snapshot Voice Session before command routing')
assert.ok(taskPost > route, 'voice transcript must still use the normal task submission API')
assert.ok(chat.includes('voice_session_id: voiceSessionId || undefined'), 'task submission must bind the snapshotted Voice Session')
assert.ok(chat.includes('if (input.autoSend) dispatchVoiceTranscript(input.text, send,'), 'auto-send must dispatch without awaiting Agent terminal state')
assert.ok(!chat.includes('async function acceptVoiceTranscription'), 'voice transcript acceptance must not inherit Agent terminal waiting')

const getUserMediaAwait = capture.indexOf('const stream = await navigator.mediaDevices.getUserMedia(')
const streamRegistration = capture.indexOf('captureSetupFenceRef.current.registerStream(setupOwner, stream)', getUserMediaAwait)
const contextCreation = capture.indexOf('const audioContext = new AudioContext()', streamRegistration)
const contextRegistration = capture.indexOf('captureSetupFenceRef.current.registerAudioContext(setupOwner, audioContext)', contextCreation)
const resumeAwait = capture.indexOf('await resumeAudioContextWithTimeout(', contextRegistration)
const resumeFence = capture.indexOf('if (!ownsSession() || !captureSetupFenceRef.current.owns(', resumeAwait)
assert.ok(getUserMediaAwait >= 0 && streamRegistration > getUserMediaAwait && streamRegistration < contextCreation, 'returned microphone stream must acquire pending ownership immediately')
assert.ok(contextRegistration > contextCreation && contextRegistration < resumeAwait, 'AudioContext must acquire pending ownership before resume')
assert.ok(resumeFence > resumeAwait, 'AudioContext resume must be followed by operation/session fencing')
assert.ok(capture.includes('captureSetupFenceRef.current.release()\n    voiceSessionIdRef.current = null'), 'cancel must synchronously release pending browser capture resources')
assert.ok(capture.includes('if (setupOwner) captureSetupFenceRef.current.release(setupOwner)'), 'setup catch/finally must release only its owned resources')
const releaseOldCapture = capture.indexOf('if (captureRef.current === resources) captureRef.current = null')
const transcriptCallback = capture.indexOf('await optionsRef.current.onTranscript(', releaseOldCapture)
assert.ok(releaseOldCapture >= 0 && transcriptCallback > releaseOldCapture, 'old capture ownership must be released before transcript handoff')

console.log('Voice Session submission and capture wiring regression test passed')
