import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Exercise the production hook, including its async callbacks and session fences.
// Only React's storage/scheduling surface, native capture, WAV decoding and the
// API transport are simulated; no microphone, model or network is used.
registerHooks({
  resolve(specifier, context, nextResolve) {
    const mockExports = specifier === 'react'
      ? ['useState', 'useRef', 'useCallback', 'useEffect']
      : specifier === '../voiceClient'
        ? ['cancelVoiceSession', 'createVoiceSession', 'getMicrophoneSettings', 'markVoicePermissionDenied', 'markVoiceRecordingStarted', 'reportVoiceRecordingLevel', 'streamVoiceEvents', 'transcribeVoiceAudio', 'updateMicrophoneSettings']
        : null
    if (mockExports) {
      const owner = specifier === 'react' ? 'hooks' : 'api'
      const source = mockExports.map(name => `export const ${name} = (...args) => globalThis.voiceHookTest.${owner}.${name}(...args)`).join('\n')
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` }
    }
    if (specifier === '../voiceAudio') return { shortCircuit: true, url: 'data:text/javascript,export const mediaBlobToVoiceWav = async () => new Blob(["synthetic-pcm"], { type: "audio/wav" })' }
    if (specifier === '@tauri-apps/api/window') return { shortCircuit: true, url: 'data:text/javascript,export const getCurrentWindow = () => ({ onFocusChanged: async (handler) => { globalThis.voiceHookTest.nativeFocus = handler; return () => {}; } })' }
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) {
        if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) {
      return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023 } }).outputText }
    }
    return nextLoad(url, context)
  },
})

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const { useVoiceCapture } = await import('../src/hooks/useVoiceCapture.ts')
const sameDeps = (left, right) => left && right && left.length === right.length && left.every((item, index) => Object.is(item, right[index]))

function createHarness() {
  const slots = []
  const pendingEffects = []
  const tracks = []
  const contexts = []
  const streams = new Map()
  const transcriptions = new Map()
  const cancelled = []
  const transcripts = []
  const discarded = []
  let cursor = 0
  let dirty = true
  let disposed = false
  let capture
  let nextSession = 0
  function VoiceCaptureHarness() {
    return useVoiceCapture({
      conversationId: 1,
      testMode: true,
      onInterruptTts() {},
      onTranscript(value) { transcripts.push(value) },
      onDiscardTranscript(id) { discarded.push(id) },
    })
  }
  const h = {
    tracks, contexts, streams, transcriptions, cancelled, transcripts, discarded,
    hooks: {
      useState(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }
        return [slots[index].value, value => {
          const next = typeof value === 'function' ? value(slots[index].value) : value
          if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true }
        }]
      },
      useRef(initial) {
        const index = cursor++
        if (!(index in slots)) slots[index] = { current: initial }
        return slots[index]
      },
      useCallback(callback, deps) {
        const index = cursor++
        if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { value: callback, deps }
        return slots[index].value
      },
      useEffect(effect, deps) {
        const index = cursor++
        if (!slots[index] || !sameDeps(slots[index].deps, deps)) {
          const previous = slots[index]
          slots[index] = { deps, cleanup: previous?.cleanup }
          pendingEffects.push(() => {
            slots[index].cleanup?.()
            slots[index].cleanup = effect()
          })
        }
      },
    },
    api: {
      async createVoiceSession() { return { voice_session_id: `voice-${++nextSession}`, auto_send: false } },
      async cancelVoiceSession(id) { cancelled.push(id); return { voice_session_id: id, state: 'CANCELLED' } },
      async getMicrophoneSettings() { return { selected_device_id: '', auto_send: false, shortcut: 'Space' } },
      async updateMicrophoneSettings() { return {} },
      async markVoicePermissionDenied() { return {} },
      async markVoiceRecordingStarted() { return {} },
      async reportVoiceRecordingLevel() { return {} },
      streamVoiceEvents(id, onEvent, signal) {
        const pending = deferred()
        const stream = { onEvent, signal, pending }
        streams.set(id, stream)
        signal.addEventListener('abort', () => pending.resolve(0), { once: true })
        return pending.promise
      },
      transcribeVoiceAudio(id, audio, signal) {
        assert.equal(audio.type, 'audio/wav')
        const pending = deferred()
        // Deliberately allow late resolution after abort. An already-decoded
        // HTTP response can race cancellation in the same way in a WebView.
        transcriptions.set(id, { ...pending, signal })
        return pending.promise
      },
    },
    render() {
      if (!dirty || disposed) return capture
      cursor = 0
      dirty = false
      capture = VoiceCaptureHarness()
      for (const run of pendingEffects.splice(0)) run()
      return capture
    },
    get capture() { return h.render() },
    async flush() {
      // Render between turns so functional state updates and callback closure
      // replacement are exercised, rather than inspecting private hook refs.
      for (let index = 0; index < 6; index++) {
        await new Promise(resolve => setImmediate(resolve))
        h.render()
      }
    },
    emit(id, event, payload = {}) {
      // Bypass the transport's aborted signal to simulate a buffered callback.
      streams.get(id).onEvent({ id: 1, voice_session_id: id, event, payload, created_at: 'test-only' })
      h.render()
    },
    async start() {
      await h.capture.start()
      await h.flush()
      assert.equal(h.capture.state, 'recording')
      assert.equal(tracks.at(-1).stopped, false)
      return `voice-${nextSession}`
    },
    async stop(id) {
      h.capture.stop()
      assert.equal(tracks.at(-1).stopped, true, 'stop must release the microphone before transcription settles')
      await h.flush()
      assert.ok(transcriptions.has(id), 'the production stop callback must reach the transcription API')
      assert.equal(h.capture.state, 'transcribing')
    },
    async dispose() {
      await h.capture.cancel()
      for (const request of transcriptions.values()) request.reject(new DOMException('test cleanup', 'AbortError'))
      await h.flush()
      disposed = true
      for (const slot of slots) slot?.cleanup?.()
      await h.flush()
      assert.ok(tracks.every(track => track.stopped), 'every microphone track must be released')
      assert.ok(contexts.every(context => context.closed), 'every AudioContext must be closed')
      assert.equal(transcripts.length, 0, 'failed or cancelled tests must never publish a transcript')
    },
  }
  globalThis.voiceHookTest = h
  const storage = new Map()
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) }
  globalThis.window = Object.assign(new EventTarget(), { setTimeout, clearTimeout })
  globalThis.document = Object.assign(new EventTarget(), { hidden: false })
  globalThis.HTMLElement = class {}
  globalThis.requestAnimationFrame = () => 1
  globalThis.cancelAnimationFrame = () => {}
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
    mediaDevices: Object.assign(new EventTarget(), {
      async enumerateDevices() { return [] },
      async getUserMedia() {
        const track = Object.assign(new EventTarget(), { stopped: false, stop() { this.stopped = true } })
        tracks.push(track)
        return { getTracks: () => [track], getAudioTracks: () => [track] }
      },
    }),
  } })
  globalThis.AudioContext = class {
    closed = false
    destination = {}
    constructor() { contexts.push(this) }
    async resume() {}
    async close() { this.closed = true }
    createMediaStreamSource() { return { connect() {}, disconnect() {} } }
    createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} } }
    createAnalyser() { return { fftSize: 512, connect() {}, disconnect() {}, getByteTimeDomainData(values) { values.fill(128) } } }
  }
  globalThis.MediaRecorder = class extends EventTarget {
    static isTypeSupported() { return true }
    state = 'inactive'
    mimeType = 'audio/webm'
    start() { this.state = 'recording' }
    stop() {
      if (this.state === 'inactive') return
      this.state = 'inactive'
      queueMicrotask(() => {
        this.dispatchEvent(Object.assign(new Event('dataavailable'), { data: new Blob(['synthetic-recording']) }))
        this.dispatchEvent(new Event('stop'))
      })
    }
  }
  h.render()
  return h
}

function ramError() {
  return Object.assign(new Error('available RAM 617377792 bytes is below the safe threshold 2147483648 bytes'), {
    detail: { code: 'RESOURCE_RAM_PRESSURE', resource: { kind: 'ram', available_bytes: 617377792, minimum_available_bytes: 2147483648 } },
  })
}

function assertRamFailure(h, withAmounts = true) {
  assert.equal(h.capture.state, 'error')
  assert.match(h.capture.error, /可用内存不足/)
  assert.match(h.capture.error, /释放内存后重新录音/)
  assert.doesNotMatch(h.capture.error, /available RAM|语音输入已取消/)
  if (withAmounts) {
    assert.match(h.capture.error, /0\.57 GiB/)
    assert.match(h.capture.error, /2\.00 GiB/)
  }
}

// HTTP first: abort/fence the SSE stream before a buffered terminal callback.
{
  const h = createHarness()
  const id = await h.start()
  await h.stop(id)
  h.transcriptions.get(id).reject(ramError())
  await h.flush()
  assertRamFailure(h)
  const message = h.capture.error
  assert.equal(h.streams.get(id).signal.aborted, true)
  h.emit(id, 'STT_FAILED', { code: 'RESOURCE_RAM_PRESSURE' })
  h.emit(id, 'VOICE_SESSION_COMPLETED', { status: 'FAILED' })
  assert.equal(h.capture.error, message, 'late terminal events must not erase the detailed HTTP failure')
  await h.start()
  assert.equal(h.capture.error, '', 'a fresh retry must clear the previous resource error')
  await h.dispose()
}

// SSE first: terminal hardware teardown is a failure, not user cancellation.
{
  const h = createHarness()
  const id = await h.start()
  await h.stop(id)
  h.emit(id, 'STT_FAILED', { code: 'RESOURCE_RAM_PRESSURE' })
  h.emit(id, 'VOICE_SESSION_COMPLETED', { status: 'FAILED' })
  assertRamFailure(h, false)
  h.transcriptions.get(id).reject(ramError())
  await h.flush()
  assertRamFailure(h)
  await h.dispose()
}

// An aborted HTTP response after an explicit FAILED event preserves its cause.
{
  const h = createHarness()
  const id = await h.start()
  await h.stop(id)
  h.emit(id, 'STT_FAILED', { code: 'RESOURCE_RAM_PRESSURE' })
  h.emit(id, 'VOICE_SESSION_COMPLETED', { status: 'FAILED' })
  h.transcriptions.get(id).reject(new DOMException('transport aborted', 'AbortError'))
  await h.flush()
  assertRamFailure(h, false)
  await h.dispose()
}

// A retry may start after SSE failure while the old HTTP error is still in flight.
{
  const h = createHarness()
  const first = await h.start()
  await h.stop(first)
  h.emit(first, 'STT_FAILED', { code: 'RESOURCE_RAM_PRESSURE' })
  h.emit(first, 'VOICE_SESSION_COMPLETED', { status: 'FAILED' })
  const next = await h.start()
  h.transcriptions.get(first).reject(ramError())
  await h.flush()
  assert.equal(h.capture.state, 'recording')
  assert.equal(h.capture.error, '')
  assert.equal(h.streams.get(next).signal.aborted, false, 'old HTTP settlement must not abort the new stream')
  assert.equal(h.tracks.at(-1).stopped, false, 'old HTTP settlement must not release the new microphone')
  await h.dispose()
}

// User cancel wins over a late HTTP error and buffered old-session SSE events.
{
  const h = createHarness()
  const first = await h.start()
  await h.stop(first)
  await h.capture.cancel()
  assert.equal(h.transcriptions.get(first).signal.aborted, true)
  assert.equal(h.capture.state, 'idle')
  const next = await h.start()
  h.transcriptions.get(first).reject(ramError())
  h.emit(first, 'STT_FAILED', { code: 'RESOURCE_RAM_PRESSURE' })
  h.emit(first, 'VOICE_SESSION_COMPLETED', { status: 'FAILED' })
  await h.flush()
  assert.equal(h.capture.state, 'recording')
  assert.equal(h.capture.error, '')
  assert.equal(h.streams.get(next).signal.aborted, false)
  assert.equal(h.tracks.at(-1).stopped, false)
  await h.dispose()
}

console.log('Voice capture production-hook HTTP/SSE failure, retry, cancellation and input-release tests passed')
