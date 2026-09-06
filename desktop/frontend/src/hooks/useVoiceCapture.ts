import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelVoiceSession, createVoiceSession, getMicrophoneSettings, markVoicePermissionDenied, markVoiceRecordingStarted, reportVoiceRecordingLevel, streamVoiceEvents, transcribeVoiceAudio, updateMicrophoneSettings, type VoiceStreamEvent } from '../voiceClient'
import { mediaBlobToVoiceWav } from '../voiceAudio'
import { VoiceCaptureSetupFence, resumeAudioContextWithTimeout, type VoiceCaptureSetupToken } from '../voiceCaptureSetupFence'
import { VoiceSseSessionFence, type VoiceSseStreamToken } from '../voiceSseSessionFence'
import { observeMicrophonePermission, resolveMicrophoneDeviceId, WINDOWS_DEFAULT_MICROPHONE_LABEL, type MicrophonePermissionState } from '../microphoneUiPolicy'
import { shouldAutoSendVoiceTranscript } from '../voiceAutoSendPolicy'
import { shouldCancelVoiceForPrivacy } from '../voiceCapturePrivacy'

export type VoiceCaptureState = 'idle' | 'requesting_permission' | 'recording' | 'processing' | 'transcribing' | 'reviewing' | 'error'

export interface VoiceInputDevice {
  id: string
  label: string
}

export interface VoiceTranscriptInput {
  voiceSessionId: string
  text: string
  autoSend: boolean
}

interface UseVoiceCaptureOptions {
  conversationId: number | null
  boundVoiceSessionId?: string | null
  testMode?: boolean
  onInterruptTts: () => Promise<void> | void
  onTranscript: (input: VoiceTranscriptInput) => Promise<void> | void
  onDiscardTranscript: (voiceSessionId: string) => void
}

interface CaptureResources {
  ownerOperation: number
  voiceSessionId: string
  stream: MediaStream
  recorder: MediaRecorder
  audioContext: AudioContext
  analyser: AnalyserNode
  source: MediaStreamAudioSourceNode
  mutedOutput: GainNode
  chunks: Blob[]
  startedAt: number
  lastReportedLevelAt: number
  meterFrame: number | null
  durationTimer: number | null
  transcribeController: AbortController | null
  finalized: boolean
  cancelled: boolean
  inputReleased: boolean
  released: boolean
  trackEndedHandlers: Array<{ track: MediaStreamTrack; handler: () => void }>
}

const MAX_RECORDING_MS = 120_000
const AUDIO_CONTEXT_RESUME_TIMEOUT_MS = 5_000
const SAVED_DEVICE_KEY = 'siyi_voice_input_device'
const SAVED_AUTO_SEND_KEY = 'siyi_voice_input_auto_send'
const SAVED_SHORTCUT_KEY = 'siyi_voice_input_shortcut'
const DEFAULT_SHORTCUT = 'Space'

function matchesVoiceShortcut(event: KeyboardEvent, shortcut: string): boolean {
  if (shortcut === 'Alt+R') return event.altKey && !event.ctrlKey && !event.metaKey && event.code === 'KeyR'
  if (shortcut === 'Enter') return !event.altKey && !event.ctrlKey && !event.metaKey && event.code === 'Enter'
  return !event.altKey && !event.ctrlKey && !event.metaKey && event.code === 'Space'
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

function userFacingError(error: unknown): string {
  const name = error instanceof DOMException ? error.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return '麦克风权限被拒绝。请在 Windows 和司忆的权限设置中允许麦克风后重试。'
  if (name === 'NotFoundError') return '没有发现可用麦克风。请连接设备后重试。'
  if (name === 'NotReadableError') return '麦克风正在被其他程序占用。请关闭占用程序后重试。'
  if (name === 'AbortError') return '语音输入已取消。'
  return error instanceof Error && error.message ? error.message : '语音输入失败，请重试。'
}

function isPermissionDenied(error: unknown): boolean {
  return error instanceof DOMException && (error.name === 'NotAllowedError' || error.name === 'SecurityError')
}

function isCancelledTranscriptionError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  if (!error || typeof error !== 'object') return false
  const candidate = error as { detail?: unknown; code?: unknown }
  const detail = candidate.detail && typeof candidate.detail === 'object' ? candidate.detail as { code?: unknown } : null
  const code = String(detail?.code || candidate.code || '')
  return code === 'STT_ALREADY_CANCELLED' || code === 'STT_CANCELLED' || code === 'VOICE_SESSION_CANCELLED'
}

function supportedRecorderOptions(): MediaRecorderOptions | undefined {
  const preferred = ['audio/webm;codecs=opus', 'audio/webm']
  const mimeType = preferred.find(candidate => MediaRecorder.isTypeSupported(candidate))
  return mimeType ? { mimeType } : undefined
}

function releaseCaptureInput(resources: CaptureResources) {
  if (resources.inputReleased) return
  resources.inputReleased = true
  if (resources.meterFrame !== null) cancelAnimationFrame(resources.meterFrame)
  if (resources.durationTimer !== null) window.clearTimeout(resources.durationTimer)
  for (const { track, handler } of resources.trackEndedHandlers) track.removeEventListener('ended', handler)
  resources.trackEndedHandlers = []
  try { resources.source.disconnect() } catch { /* already disconnected */ }
  try { resources.analyser.disconnect() } catch { /* already disconnected */ }
  try { resources.mutedOutput.disconnect() } catch { /* already disconnected */ }
  for (const track of resources.stream.getTracks()) {
    try { track.stop() } catch { /* continue releasing every microphone track */ }
  }
}

function releaseCaptureHardware(resources: CaptureResources) {
  if (resources.released) return
  resources.released = true
  // Keep the AudioContext alive until the already-recorded Blob has been
  // decoded into the controlled PCM WAV.  The microphone tracks, meter and
  // input graph are released immediately on stop, not after slow decoding.
  releaseCaptureInput(resources)
  void resources.audioContext.close().catch(() => undefined)
}

export function useVoiceCapture(options: UseVoiceCaptureOptions) {
  const [state, setState] = useState<VoiceCaptureState>('idle')
  const [error, setError] = useState('')
  const [elapsedMs, setElapsedMs] = useState(0)
  const [level, setLevel] = useState(0)
  const [permission, setPermission] = useState<MicrophonePermissionState>('unknown')
  const [devices, setDevices] = useState<VoiceInputDevice[]>([])
  const [selectedDeviceId, setSelectedDeviceIdState] = useState(() => localStorage.getItem(SAVED_DEVICE_KEY) || '')
  const [autoSend, setAutoSendState] = useState(() => localStorage.getItem(SAVED_AUTO_SEND_KEY) === 'true')
  const [shortcut, setShortcutState] = useState(() => localStorage.getItem(SAVED_SHORTCUT_KEY) || DEFAULT_SHORTCUT)
  const captureRef = useRef<CaptureResources | null>(null)
  const captureSetupFenceRef = useRef(new VoiceCaptureSetupFence())
  const voiceSessionIdRef = useRef<string | null>(null)
  const voiceEventsControllerRef = useRef<AbortController | null>(null)
  const voiceEventsCursorRef = useRef(0)
  const voiceEventsFenceRef = useRef(new VoiceSseSessionFence())
  const selectedDeviceIdRef = useRef(selectedDeviceId)
  const autoSendRef = useRef(autoSend)
  const shortcutRef = useRef(shortcut)
  const preferencesTouchedRef = useRef(false)
  const operationRef = useRef(0)
  // A push-to-talk release can happen while getUserMedia or session creation
  // is still awaiting.  Keep a synchronous fence outside React state so that
  // a quick release cannot later start an unnoticed recording.
  const pendingStartRef = useRef(false)
  const shortcutHeldRef = useRef(false)
  const cancelRef = useRef<() => Promise<void>>(async () => undefined)
  const mountedRef = useRef(true)
  const optionsRef = useRef(options)
  optionsRef.current = options

  useEffect(() => { selectedDeviceIdRef.current = selectedDeviceId }, [selectedDeviceId])
  useEffect(() => { autoSendRef.current = autoSend }, [autoSend])
  useEffect(() => { shortcutRef.current = shortcut }, [shortcut])

  const stopVoiceEventStream = useCallback((voiceSessionId?: string) => {
    const owner = voiceEventsFenceRef.current.activeOwner()
    if (!owner || (voiceSessionId && owner.voiceSessionId !== voiceSessionId)) return
    if (!voiceEventsFenceRef.current.clear(owner)) return
    if (voiceEventsControllerRef.current) voiceEventsControllerRef.current.abort()
    voiceEventsControllerRef.current = null
  }, [])

  const handleVoiceEvent = useCallback((owner: VoiceSseStreamToken, event: VoiceStreamEvent) => {
    const sessionId = event.voice_session_id
    // An aborted stream can still deliver an event that had already been
    // decoded. It must not mutate a session that has since replaced it.
    if (
      sessionId !== owner.voiceSessionId ||
      !voiceEventsFenceRef.current.owns(owner, voiceSessionIdRef.current)
    ) return
    const eventStatus = String(event.payload.status || '')
    if (event.event === 'AUDIO_READY' && voiceSessionIdRef.current === sessionId && mountedRef.current) setState('processing')
    if (event.event === 'STT_STARTED' && voiceSessionIdRef.current === sessionId && mountedRef.current) setState('transcribing')
    if (event.event === 'MESSAGE_READY' && voiceSessionIdRef.current === sessionId && mountedRef.current) setState('reviewing')
    if (event.event === 'MIC_PERMISSION' && eventStatus === 'DENIED' && voiceSessionIdRef.current === sessionId && mountedRef.current) {
      setState('error')
      setError('麦克风权限被拒绝。请在 Windows 和司忆的权限设置中允许麦克风后重试。')
    }
    if (event.event === 'STT_FAILED' && voiceSessionIdRef.current === sessionId && mountedRef.current) {
      const code = String(event.payload.code || 'STT_TRANSCRIPTION_FAILED')
      setState('error')
      setError(`本地语音转写失败（${code}）。`)
    }
    if (event.event !== 'STT_CANCELLED' && event.event !== 'VOICE_SESSION_COMPLETED') return
    const terminal = event.event === 'STT_CANCELLED' || ['CANCELLED', 'FAILED', 'COMPLETED'].includes(eventStatus)
    if (!terminal || voiceSessionIdRef.current !== sessionId) return
    if (event.event === 'VOICE_SESSION_COMPLETED') voiceEventsFenceRef.current.markTerminal(owner, voiceSessionIdRef.current)
    const resources = captureRef.current
    if (resources?.voiceSessionId === sessionId) {
      resources.cancelled = true
      if (resources.recorder.state !== 'inactive') resources.recorder.stop()
      releaseCaptureHardware(resources)
      captureRef.current = null
    }
    captureSetupFenceRef.current.release()
    voiceSessionIdRef.current = null
    optionsRef.current.onDiscardTranscript(sessionId)
    if (!mountedRef.current) return
    setElapsedMs(0)
    setLevel(0)
    if (eventStatus === 'FAILED') {
      setState('error')
      setError('语音输入未完成，请检查麦克风或本地转写状态后重试。')
    } else {
      setState('idle')
      setError('')
    }
  }, [])

  const startVoiceEventStream = useCallback((voiceSessionId: string) => {
    stopVoiceEventStream()
    const controller = new AbortController()
    const owner = voiceEventsFenceRef.current.begin(voiceSessionId)
    voiceEventsControllerRef.current = controller
    voiceEventsCursorRef.current = 0
    const ownsStream = () => (
      !controller.signal.aborted &&
      voiceEventsControllerRef.current === controller &&
      voiceEventsFenceRef.current.owns(owner, voiceSessionIdRef.current)
    )
    void (async () => {
      let reconnects = 0
      while (ownsStream()) {
        try {
          voiceEventsCursorRef.current = await streamVoiceEvents(
            voiceSessionId,
            event => handleVoiceEvent(owner, event),
            controller.signal,
            voiceEventsCursorRef.current,
          )
          if (!ownsStream() || voiceEventsFenceRef.current.isTerminal(owner)) return
          throw new Error('本地语音状态连接意外结束。')
        } catch (caught) {
          if (!ownsStream()) return
          reconnects += 1
          const resources = captureRef.current
          const inputActive = Boolean(resources && !resources.cancelled && !resources.inputReleased && !resources.released)
          const mustFailClosed = voiceEventsFenceRef.current.shouldFailClosed(owner, voiceSessionIdRef.current, {
            inputActive,
            startPending: pendingStartRef.current,
            reconnectLimitReached: reconnects > 2,
          })
          if (!mustFailClosed) {
            await new Promise(resolve => window.setTimeout(resolve, reconnects * 300))
            if (!ownsStream()) return
            continue
          }
          // Fail closed: a recorder must never keep capturing once the local
          // state authority is unreachable. cancel() also releases local
          // tracks when the backend itself is already unavailable.
          if (!ownsStream()) return
          const cancellationOperation = operationRef.current
          await cancelRef.current()
          // cancel() advances operationRef before its first await. Do not let
          // this old stream overwrite UI state if a new capture began while
          // backend cancellation was still in flight.
          if (mountedRef.current && operationRef.current === cancellationOperation + 1) {
            setState('error')
            setError(`语音状态连接中断，已为保护隐私停止录音：${userFacingError(caught)}`)
          }
          return
        }
      }
    })()
      .finally(() => {
        if (voiceEventsFenceRef.current.clear(owner) && voiceEventsControllerRef.current === controller) {
          voiceEventsControllerRef.current = null
        }
      })
  }, [handleVoiceEvent, stopVoiceEventStream])

  const refreshDevices = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) {
      setDevices([])
      return []
    }
    const inputs = (await navigator.mediaDevices.enumerateDevices())
      .filter(device => device.kind === 'audioinput' && device.deviceId && device.deviceId !== 'default')
      .map((device, index) => ({ id: device.deviceId, label: device.label || `麦克风 ${index + 1}` }))
    if (!mountedRef.current) return inputs
    setDevices(inputs)
    setSelectedDeviceIdState(current => {
      const next = resolveMicrophoneDeviceId(current, inputs)
      selectedDeviceIdRef.current = next
      return next
    })
    return inputs
  }, [])

  const setSelectedDeviceId = useCallback((deviceId: string) => {
    preferencesTouchedRef.current = true
    const selected = devices.find(device => device.id === deviceId)
    localStorage.setItem(SAVED_DEVICE_KEY, deviceId)
    selectedDeviceIdRef.current = deviceId
    setSelectedDeviceIdState(deviceId)
    void updateMicrophoneSettings({
      selected_device_id: deviceId,
      selected_device_label: selected?.label || WINDOWS_DEFAULT_MICROPHONE_LABEL,
      auto_send: autoSendRef.current,
    }).catch(() => undefined)
  }, [devices])

  const setAutoSend = useCallback((value: boolean) => {
    preferencesTouchedRef.current = true
    const selected = devices.find(device => device.id === selectedDeviceIdRef.current)
    localStorage.setItem(SAVED_AUTO_SEND_KEY, String(value))
    autoSendRef.current = value
    setAutoSendState(value)
    void updateMicrophoneSettings({
      selected_device_id: selectedDeviceIdRef.current,
      selected_device_label: selected?.label || WINDOWS_DEFAULT_MICROPHONE_LABEL,
      auto_send: value,
    }).catch(() => undefined)
  }, [devices])

  const setShortcut = useCallback((value: string) => {
    const next = ['Space', 'Enter', 'Alt+R'].includes(value) ? value : DEFAULT_SHORTCUT
    preferencesTouchedRef.current = true
    localStorage.setItem(SAVED_SHORTCUT_KEY, next)
    shortcutRef.current = next
    setShortcutState(next)
    void updateMicrophoneSettings({ shortcut: next }).catch(() => undefined)
  }, [])

  const cancel = useCallback(async () => {
    const cancellationOperation = ++operationRef.current
    pendingStartRef.current = false
    shortcutHeldRef.current = false
    const resources = captureRef.current
    const voiceSessionId = voiceSessionIdRef.current
    if (resources) {
      resources.cancelled = true
      resources.transcribeController?.abort()
      if (resources.recorder.state !== 'inactive') resources.recorder.stop()
      releaseCaptureHardware(resources)
      captureRef.current = null
    }
    captureSetupFenceRef.current.release()
    voiceSessionIdRef.current = null
    stopVoiceEventStream(voiceSessionId || undefined)
    if (voiceSessionId) await cancelVoiceSession(voiceSessionId).catch(() => undefined)
    if (operationRef.current !== cancellationOperation) return
    if (voiceSessionId) optionsRef.current.onDiscardTranscript(voiceSessionId)
    if (mountedRef.current) {
      setState('idle')
      setElapsedMs(0)
      setLevel(0)
      setError('')
    }
  }, [stopVoiceEventStream])
  cancelRef.current = cancel

  const finishRecording = useCallback(async (resources: CaptureResources, voiceSessionId: string) => {
    if (resources.finalized) return
    resources.finalized = true
    try {
      if (resources.cancelled) return
      if (mountedRef.current) setState('processing')
      releaseCaptureInput(resources)
      const rawAudio = new Blob(resources.chunks, { type: resources.recorder.mimeType || 'audio/webm' })
      resources.chunks = []
      const wav = await mediaBlobToVoiceWav(rawAudio, resources.audioContext)
      if (resources.cancelled || voiceSessionIdRef.current !== voiceSessionId) return
      releaseCaptureHardware(resources)
      resources.transcribeController = new AbortController()
      if (mountedRef.current) setState('transcribing')
      const response = await transcribeVoiceAudio(voiceSessionId, wav, resources.transcribeController.signal)
      if (resources.cancelled || voiceSessionIdRef.current !== voiceSessionId) return
      const text = response.transcription.text.trim()
      if (!text) throw new Error('未识别到可发送的语音内容。')
      if (mountedRef.current) setState('reviewing')
      // The recorded input no longer owns browser capture resources. Clear it
      // before notifying chat so a non-blocking auto-send can hand off the
      // Voice Session without waiting for the Agent task to finish.
      if (captureRef.current === resources) captureRef.current = null
      const autoSend = shouldAutoSendVoiceTranscript(
        optionsRef.current.testMode ? false : response.session.auto_send,
        response.transcription,
      )
      await optionsRef.current.onTranscript({ voiceSessionId, text, autoSend })
      if (optionsRef.current.testMode) await cancelRef.current()
    } catch (caught) {
      if (resources.cancelled || isCancelledTranscriptionError(caught)) {
        if (voiceSessionIdRef.current === voiceSessionId) voiceSessionIdRef.current = null
        if (mountedRef.current && operationRef.current === resources.ownerOperation) setState('idle')
        return
      }
      if (voiceSessionIdRef.current === voiceSessionId) voiceSessionIdRef.current = null
      await cancelVoiceSession(voiceSessionId).catch(() => undefined)
      if (mountedRef.current && operationRef.current === resources.ownerOperation) {
        setState('error')
        setError(userFacingError(caught))
      }
    } finally {
      releaseCaptureHardware(resources)
      if (captureRef.current === resources) captureRef.current = null
      if (mountedRef.current && operationRef.current === resources.ownerOperation) {
        setElapsedMs(0)
        setLevel(0)
      }
    }
  }, [])

  const stop = useCallback(() => {
    const resources = captureRef.current
    const voiceSessionId = voiceSessionIdRef.current
    if (!resources || !voiceSessionId) {
      // Treat release during permission/session setup as cancellation, rather
      // than letting the outstanding start operation acquire the microphone.
      if (pendingStartRef.current) void cancel()
      return
    }
    if (resources.cancelled) return
    if (resources.recorder.state === 'inactive') return
    if (mountedRef.current) setState('processing')
    resources.recorder.stop()
    releaseCaptureInput(resources)
  }, [cancel])

  const start = useCallback(async () => {
    const currentOptions = optionsRef.current
    if (!currentOptions.conversationId || pendingStartRef.current || captureSetupFenceRef.current.hasOwner() || captureRef.current || voiceSessionIdRef.current || state === 'reviewing') return
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined' || typeof AudioContext === 'undefined') {
      setState('error')
      setError('当前桌面运行环境不支持 WebView2 麦克风录音。')
      return
    }
    const operation = ++operationRef.current
    pendingStartRef.current = true
    setError('')
    setElapsedMs(0)
    setLevel(0)
    setState('requesting_permission')
    let voiceSessionId: string | null = null
    let setupOwner: VoiceCaptureSetupToken | null = null
    const ownsOperation = () => mountedRef.current && pendingStartRef.current && operation === operationRef.current
    const ownsSession = () => ownsOperation() && voiceSessionId !== null && voiceSessionIdRef.current === voiceSessionId
    try {
      await currentOptions.onInterruptTts()
      if (!ownsOperation()) return
      const session = await createVoiceSession(currentOptions.conversationId, selectedDeviceId, currentOptions.testMode ? false : autoSendRef.current)
      voiceSessionId = session.voice_session_id
      // If push-to-talk was released while the session request was in flight,
      // this client did not yet hold the id for cancel().  Close the newly
      // created server session before any microphone or SSE work begins.
      if (!ownsOperation()) {
        await cancelVoiceSession(voiceSessionId).catch(() => undefined)
        return
      }
      voiceSessionIdRef.current = voiceSessionId
      startVoiceEventStream(voiceSessionId)
      setupOwner = captureSetupFenceRef.current.begin(operation, voiceSessionId)
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          deviceId: selectedDeviceId ? { exact: selectedDeviceId } : undefined,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      if (mountedRef.current) setPermission('granted')
      // Register synchronously in the same continuation that receives the
      // stream. A cancel that won the race has already retired setupOwner, so
      // registerStream stops every late track immediately.
      if (!captureSetupFenceRef.current.registerStream(setupOwner, stream)) return
      if (!ownsSession() || !captureSetupFenceRef.current.owns(setupOwner, operationRef.current, voiceSessionIdRef.current)) {
        captureSetupFenceRef.current.release(setupOwner)
        return
      }
      const audioContext = new AudioContext()
      if (!captureSetupFenceRef.current.registerAudioContext(setupOwner, audioContext)) return
      await resumeAudioContextWithTimeout(audioContext, AUDIO_CONTEXT_RESUME_TIMEOUT_MS)
      if (!ownsSession() || !captureSetupFenceRef.current.owns(setupOwner, operationRef.current, voiceSessionIdRef.current)) {
        captureSetupFenceRef.current.release(setupOwner)
        return
      }
      const source = audioContext.createMediaStreamSource(stream)
      const analyser = audioContext.createAnalyser()
      analyser.fftSize = 512
      const mutedOutput = audioContext.createGain()
      mutedOutput.gain.value = 0
      source.connect(analyser)
      analyser.connect(mutedOutput)
      mutedOutput.connect(audioContext.destination)
      const recorder = new MediaRecorder(stream, supportedRecorderOptions())
      const resources: CaptureResources = {
        ownerOperation: operation,
        voiceSessionId,
        stream,
        recorder,
        audioContext,
        analyser,
        source,
        mutedOutput,
        chunks: [],
        startedAt: Date.now(),
        lastReportedLevelAt: 0,
        meterFrame: null,
        durationTimer: null,
        transcribeController: null,
        finalized: false,
        cancelled: false,
        inputReleased: false,
        released: false,
        trackEndedHandlers: [],
      }
      recorder.addEventListener('dataavailable', event => { if (event.data.size) resources.chunks.push(event.data) })
      recorder.addEventListener('stop', () => { void finishRecording(resources, voiceSessionId!) })
      const handleTrackEnded = () => {
        if (resources.released || resources.cancelled) return
        resources.cancelled = true
        releaseCaptureHardware(resources)
        captureRef.current = null
        voiceSessionIdRef.current = null
        void cancelVoiceSession(voiceSessionId!).catch(() => undefined)
        if (mountedRef.current) {
          setState('error')
          setError('麦克风连接已中断，录音已停止。')
        }
      }
      for (const track of stream.getAudioTracks()) {
        track.addEventListener('ended', handleTrackEnded)
        resources.trackEndedHandlers.push({ track, handler: handleTrackEnded })
      }
      captureRef.current = resources
      if (!captureSetupFenceRef.current.complete(setupOwner)) {
        resources.cancelled = true
        releaseCaptureHardware(resources)
        captureRef.current = null
        return
      }
      await markVoiceRecordingStarted(voiceSessionId, selectedDeviceId)
      if (resources.cancelled || captureRef.current !== resources || !ownsSession()) return
      const meter = () => {
        if (resources.released || resources.cancelled) return
        const values = new Uint8Array(resources.analyser.fftSize)
        resources.analyser.getByteTimeDomainData(values)
        let total = 0
        for (const value of values) {
          const normalized = (value - 128) / 128
          total += normalized * normalized
        }
        const liveLevel = Math.min(1, Math.sqrt(total / values.length) * 3)
        if (mountedRef.current) {
          setLevel(liveLevel)
          setElapsedMs(Date.now() - resources.startedAt)
        }
        const sampledAt = Date.now()
        if (sampledAt - resources.lastReportedLevelAt >= 1_000) {
          resources.lastReportedLevelAt = sampledAt
          void reportVoiceRecordingLevel(voiceSessionId!, liveLevel).catch(() => undefined)
        }
        resources.meterFrame = requestAnimationFrame(meter)
      }
      resources.durationTimer = window.setTimeout(() => {
        if (captureRef.current === resources && resources.recorder.state !== 'inactive') {
          setError('已达到 120 秒录音上限，正在转写已录内容。')
          stop()
        }
      }, MAX_RECORDING_MS)
      recorder.start()
      meter()
      await refreshDevices()
      if (captureRef.current !== resources || resources.cancelled || !ownsSession()) return
      setState('recording')
    } catch (caught) {
      if (setupOwner) captureSetupFenceRef.current.release(setupOwner)
      const resources = captureRef.current
      if (resources?.ownerOperation === operation && resources.voiceSessionId === voiceSessionId) {
        resources.cancelled = true
        if (resources.recorder.state !== 'inactive') resources.recorder.stop()
        releaseCaptureHardware(resources)
        captureRef.current = null
      }
      if (voiceSessionId) {
        const sessionId = voiceSessionId
        stopVoiceEventStream(sessionId)
        if (isPermissionDenied(caught)) await markVoicePermissionDenied(sessionId).catch(() => cancelVoiceSession(sessionId).catch(() => undefined))
        else await cancelVoiceSession(sessionId).catch(() => undefined)
      }
      if (voiceSessionIdRef.current === voiceSessionId) voiceSessionIdRef.current = null
      if (mountedRef.current && operation === operationRef.current) {
        if (isPermissionDenied(caught)) setPermission('denied')
        setState('error')
        setError(userFacingError(caught))
      }
    } finally {
      if (setupOwner) captureSetupFenceRef.current.release(setupOwner)
      if (operation === operationRef.current) pendingStartRef.current = false
    }
  }, [finishRecording, refreshDevices, selectedDeviceId, startVoiceEventStream, state, stop, stopVoiceEventStream])

  useEffect(() => {
    let disposed = false
    let stopObserving: () => void = () => undefined
    void observeMicrophonePermission(navigator.permissions, next => {
      if (!disposed && mountedRef.current) setPermission(next)
    }).then(stop => {
      if (disposed) stop()
      else stopObserving = stop
    })
    return () => {
      disposed = true
      stopObserving()
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refreshDevices()
    void getMicrophoneSettings().then(settings => {
      if (!mountedRef.current || preferencesTouchedRef.current) return
      const savedDevice = localStorage.getItem(SAVED_DEVICE_KEY)
      if (settings.selected_device_id || !savedDevice) {
        selectedDeviceIdRef.current = settings.selected_device_id
        localStorage.setItem(SAVED_DEVICE_KEY, settings.selected_device_id)
        setSelectedDeviceIdState(settings.selected_device_id)
        void refreshDevices()
      }
      const savedAutoSend = localStorage.getItem(SAVED_AUTO_SEND_KEY)
      if (settings.selected_device_id || savedAutoSend === null) {
        autoSendRef.current = settings.auto_send
        localStorage.setItem(SAVED_AUTO_SEND_KEY, String(settings.auto_send))
        setAutoSendState(settings.auto_send)
      }
      const savedShortcut = localStorage.getItem(SAVED_SHORTCUT_KEY)
      if (!savedShortcut || settings.shortcut) {
        const nextShortcut = ['Space', 'Enter', 'Alt+R'].includes(settings.shortcut) ? settings.shortcut : DEFAULT_SHORTCUT
        shortcutRef.current = nextShortcut
        localStorage.setItem(SAVED_SHORTCUT_KEY, nextShortcut)
        setShortcutState(nextShortcut)
      }
    }).catch(() => undefined)
    const devices = navigator.mediaDevices
    const onDeviceChange = () => { void refreshDevices() }
    devices?.addEventListener?.('devicechange', onDeviceChange)
    return () => {
      mountedRef.current = false
      devices?.removeEventListener?.('devicechange', onDeviceChange)
      void cancel()
    }
  }, [cancel, refreshDevices])

  useEffect(() => {
    if (!options.boundVoiceSessionId || options.boundVoiceSessionId !== voiceSessionIdRef.current) return
    voiceSessionIdRef.current = null
    setState('idle')
    setElapsedMs(0)
    setLevel(0)
    setError('')
  }, [options.boundVoiceSessionId])

  useEffect(() => {
    const stopAll = () => { void cancel() }
    const cancelWithEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || (!voiceSessionIdRef.current && !pendingStartRef.current)) return
      event.preventDefault()
      void cancel()
    }
    const stopForPrivacy = () => {
      const resources = captureRef.current
      // Losing focus must fail closed while the microphone or a pending
      // getUserMedia operation can still acquire input. Once stop() has
      // released every track, the already-recorded WAV may safely finish
      // local transcription without keeping the microphone open.
      if (!shouldCancelVoiceForPrivacy({
        capturePresent: Boolean(resources),
        inputReleased: resources?.inputReleased ?? true,
        setupPending: captureSetupFenceRef.current.hasOwner(),
        startPending: pendingStartRef.current,
      })) return
      void cancel()
    }
    const stopWhenHidden = () => {
      if (document.hidden) stopForPrivacy()
    }
    let disposed = false
    let unlistenWindowFocus: (() => void) | undefined
    // WebView visibility normally covers minimize.  The native focus hook is
    // deliberately a second fail-closed guard for Windows/Tauri variants that
    // do not propagate a visibilitychange until after the window is hidden.
    void import('@tauri-apps/api/window')
      .then(async ({ getCurrentWindow }) => getCurrentWindow().onFocusChanged(({ payload: focused }) => {
        if (!focused) stopForPrivacy()
      }))
      .then(unlisten => {
        if (disposed) unlisten()
        else unlistenWindowFocus = unlisten
      })
      .catch(() => undefined)
    window.addEventListener('siyi:voice-stop', stopAll)
    window.addEventListener('keydown', cancelWithEscape)
    window.addEventListener('pagehide', stopAll)
    document.addEventListener('visibilitychange', stopWhenHidden)
    return () => {
      disposed = true
      window.removeEventListener('siyi:voice-stop', stopAll)
      window.removeEventListener('keydown', cancelWithEscape)
      window.removeEventListener('pagehide', stopAll)
      document.removeEventListener('visibilitychange', stopWhenHidden)
      unlistenWindowFocus?.()
    }
  }, [cancel])

  useEffect(() => {
    if (options.testMode) return
    const keyDown = (event: KeyboardEvent) => {
      if (event.repeat || event.isComposing || isEditableTarget(event.target) || !matchesVoiceShortcut(event, shortcutRef.current)) return
      if (state !== 'idle' && state !== 'error') return
      event.preventDefault()
      shortcutHeldRef.current = true
      void start()
    }
    const keyUp = (event: KeyboardEvent) => {
      if (!shortcutHeldRef.current || !matchesVoiceShortcut(event, shortcutRef.current)) return
      shortcutHeldRef.current = false
      event.preventDefault()
      stop()
    }
    window.addEventListener('keydown', keyDown)
    window.addEventListener('keyup', keyUp)
    return () => {
      window.removeEventListener('keydown', keyDown)
      window.removeEventListener('keyup', keyUp)
    }
  }, [options.testMode, start, state, stop])

  return {
    state,
    error,
    elapsedMs,
    level,
    permission,
    devices,
    selectedDeviceId,
    setSelectedDeviceId,
    autoSend,
    setAutoSend,
    shortcut,
    setShortcut,
    start,
    stop,
    cancel,
    refreshDevices,
  }
}
