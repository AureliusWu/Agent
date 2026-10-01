import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import {
  microphoneLevelPercent,
  microphonePermissionLabel,
  normalizeMicrophonePermission,
  observeMicrophonePermission,
  resolveMicrophoneDeviceId,
  WINDOWS_DEFAULT_MICROPHONE_LABEL,
} from '../src/microphoneUiPolicy.ts'

assert.equal(normalizeMicrophonePermission('granted'), 'granted')
assert.equal(normalizeMicrophonePermission('denied'), 'denied')
assert.equal(normalizeMicrophonePermission('prompt'), 'prompt')
assert.equal(normalizeMicrophonePermission('unsupported'), 'unknown')
assert.equal(microphonePermissionLabel('granted'), '已允许')
assert.equal(microphonePermissionLabel('denied'), '已拒绝')
assert.equal(microphonePermissionLabel('prompt'), '待询问')
assert.equal(microphonePermissionLabel('unknown'), '未知')

const devices = [{ id: 'usb-microphone', label: 'USB 麦克风' }]
assert.equal(resolveMicrophoneDeviceId('', devices), '', 'empty device id must retain Windows default')
assert.equal(resolveMicrophoneDeviceId('usb-microphone', devices), 'usb-microphone')
assert.equal(resolveMicrophoneDeviceId('disconnected-device', devices), '', 'disconnected selection must fall back to Windows default')
assert.equal(WINDOWS_DEFAULT_MICROPHONE_LABEL, 'Windows 默认麦克风')
assert.equal(microphoneLevelPercent(0), 0, 'silence must render as a real zero, not a fake minimum bar')
assert.equal(microphoneLevelPercent(-1), 0)
assert.equal(microphoneLevelPercent(Number.NaN), 0)
assert.equal(microphoneLevelPercent(0.427), 43)
assert.equal(microphoneLevelPercent(2), 100)

let permissionState: unknown = 'prompt'
let changeListener: (() => void) | undefined
let listenerRemoved = false
const observed: string[] = []
const stopObserving = await observeMicrophonePermission({
  query: async descriptor => {
    assert.equal(descriptor.name, 'microphone')
    return {
      get state() { return permissionState },
      addEventListener: (_type, listener) => { changeListener = listener },
      removeEventListener: (_type, listener) => { listenerRemoved = listener === changeListener },
    }
  },
}, state => observed.push(state))
assert.deepEqual(observed, ['prompt'])
permissionState = 'granted'
changeListener?.()
assert.deepEqual(observed, ['prompt', 'granted'])
stopObserving()
assert.equal(listenerRemoved, true)

const unknownStates: string[] = []
await observeMicrophonePermission(undefined, state => unknownStates.push(state))
await observeMicrophonePermission({ query: async () => { throw new Error('unsupported') } }, state => unknownStates.push(state))
assert.deepEqual(unknownStates, ['unknown', 'unknown'])

const capturePath = fileURLToPath(new URL('../src/hooks/useVoiceCapture.ts', import.meta.url))
const composerControlPath = fileURLToPath(new URL('../src/components/chat/VoiceInputControl.tsx', import.meta.url))
const settingsControlPath = fileURLToPath(new URL('../src/components/providers/MicrophoneSettingsControl.tsx', import.meta.url))
const localAiPath = fileURLToPath(new URL('../src/components/providers/LocalAiPanel.tsx', import.meta.url))
const [capture, composerControl, settingsControl, localAi] = await Promise.all(
  [capturePath, composerControlPath, settingsControlPath, localAiPath].map(path => readFile(path, 'utf8')),
)

assert.ok(capture.includes('currentOptions.testMode ? false : autoSendRef.current'), 'test capture must create a non-auto-send Voice Session')
assert.ok(capture.includes('shouldAutoSendVoiceTranscript('), 'transcript auto-send must use the confidence policy')
assert.ok(capture.includes('optionsRef.current.testMode ? false : response.session.auto_send'), 'test transcript must never request ordinary auto-send')
assert.ok(capture.includes('if (optionsRef.current.testMode) await cancelRef.current()'), 'test Voice Session must be cancelled and cleaned after local result delivery')
assert.ok(settingsControl.includes('testMode: true'), 'settings recording must reuse the production capture hook in test mode')
assert.ok(settingsControl.includes('onTranscript: input => setTestTranscript(input.text)'), 'test transcription must remain local to settings')
assert.ok(!settingsControl.includes('onTranscript: voice?.onTranscript'), 'settings test must not submit an ordinary chat message')
assert.ok(settingsControl.includes(`<option value="">{WINDOWS_DEFAULT_MICROPHONE_LABEL}</option>`), 'settings must always expose Windows default microphone')
assert.ok(composerControl.includes(`<option value="">{WINDOWS_DEFAULT_MICROPHONE_LABEL}</option>`), 'composer must always expose Windows default microphone')
assert.ok(!composerControl.includes('Math.max(3'), 'composer must not fake a non-zero microphone level')
assert.ok(settingsControl.includes('style={{ width: `${levelPercent}%` }}'), 'settings zero level must render at zero width')
assert.ok(localAi.includes("<strong>{sttSettings?.provider || 'STT Provider 状态未知'}</strong>"), 'settings must display the configured STT provider directly and must not call a failed diagnostic unconfigured')
assert.ok(localAi.includes("sttSettings ? (sttSettings.device === 'cuda'"), 'device display must depend on confirmed STT settings rather than inventing CPU after a failed request')

console.log('Microphone settings UI policy regression test passed')
