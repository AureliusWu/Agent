import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import {
  createPlaybackInterruptionGate,
  createPlaybackSettlement,
  waitForPlayback,
} from '../src/ttsPlaybackSettlement.ts'

const interrupted = createPlaybackSettlement()
const rejection = interrupted.promise.then(
  () => 'resolved',
  error => (error as Error).message,
)
interrupted.interrupt()
interrupted.complete()
assert.equal(await rejection, 'TTS playback was interrupted')
assert.equal(interrupted.settled(), true)

const completed = createPlaybackSettlement()
completed.complete()
await completed.promise
completed.interrupt()
assert.equal(completed.settled(), true)

// A browser may leave play() pending while it negotiates a decoder or output
// device. Interruption must settle the caller without waiting for that Promise.
const pendingPlay = createPlaybackSettlement()
const neverStarted = new Promise<void>(() => {})
const interruptedWhileStarting = waitForPlayback(pendingPlay, () => neverStarted).then(
  () => 'resolved',
  error => (error as Error).message,
)
pendingPlay.interrupt()
const timeout = new Promise<string>(resolve => setTimeout(() => resolve('timeout'), 250))
assert.equal(
  await Promise.race([interruptedWhileStarting, timeout]),
  'TTS playback was interrupted',
)

const normalPlayback = createPlaybackSettlement()
let playCalled = false
const normalResult = waitForPlayback(normalPlayback, async () => { playCalled = true })
await Promise.resolve()
assert.equal(playCalled, true)
normalPlayback.complete()
await normalResult

// An interrupt can arrive before a test player has a browser Audio object. It
// must remain sticky, avoid an early unhandled settlement rejection, and stop
// later playback preparation before it reaches play().
let interruptionCallbacks = 0
let audioWasCreated = false
const prePlayInterruption = createPlaybackInterruptionGate(() => { interruptionCallbacks += 1 })
const asynchronousPreparation = (async () => {
  await Promise.resolve()
  prePlayInterruption.throwIfInterrupted()
  audioWasCreated = true
})().then(
  () => 'resolved',
  error => (error as Error).message,
)
prePlayInterruption.interrupt()
prePlayInterruption.interrupt()
assert.equal(await asynchronousPreparation, 'TTS playback was interrupted')
assert.equal(prePlayInterruption.interrupted(), true)
assert.equal(interruptionCallbacks, 1)
assert.equal(audioWasCreated, false)

// Guard the component integration too: the subscription must be installed
// before testVoice reaches its first asynchronous /speak request.
const localAiPath = fileURLToPath(new URL('../src/components/providers/LocalAiPanel.tsx', import.meta.url))
const localAi = await readFile(localAiPath, 'utf8')
const testVoiceStart = localAi.indexOf('  const testVoice = async () => {')
const testVoiceEnd = localAi.indexOf('\n\n  return <div className="local-ai-panel">', testVoiceStart)
assert.ok(testVoiceStart >= 0 && testVoiceEnd > testVoiceStart, 'LocalAiPanel must retain the testVoice handler')
const testVoice = localAi.slice(testVoiceStart, testVoiceEnd)
assert.ok(
  testVoice.indexOf("window.addEventListener('siyi:tts-interrupt', interruption.interrupt)")
    < testVoice.indexOf("await api<{ request_id: string; audio_url: string }>('/api/tts/speak'"),
  'testVoice must subscribe to interruption before beginning asynchronous synthesis',
)
assert.ok(
  (testVoice.match(/interruption\.throwIfInterrupted\(\)/g) || []).length >= 3,
  'testVoice must fence synthesis, download, and player start against early interruption',
)

console.log('TTS playback settlement tests passed')
