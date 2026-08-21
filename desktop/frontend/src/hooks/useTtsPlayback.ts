import { useCallback, useEffect, useRef, useState } from 'react'
import { api, apiFetch } from '../api'
import {
  createPlaybackSettlement,
  type PlaybackSettlement,
  waitForPlayback,
} from '../ttsPlaybackSettlement'
import { splitStreaming, type StreamBuffer } from '../ttsStreamSplitter'

export interface TtsSettings {
  enabled: boolean
  provider: string
  fallback_provider: string
  allow_fallback: boolean
  voice: string
  speed: number
  volume: number
  playback_mode: string
  interrupt_policy: string
  cache_enabled: boolean
}

interface SpeakResult {
  request_id: string
  status: string
  audio_url: string | null
}

export function useTtsPlayback() {
  const [settings, setSettings] = useState<TtsSettings | null>(null)
  const buffersRef = useRef(new Map<string, StreamBuffer>())
  const sequenceRef = useRef(Promise.resolve())
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const playbackSettlementRef = useRef<PlaybackSettlement | null>(null)
  const generationRef = useRef(0)
  // The server reserves a voice-bound session when it emits a model delta.
  // Track queued browser dispatches so that the reservation spans the gaps
  // between sequential audio sentences and is released deterministically.
  const pendingDispatchesRef = useRef(new Map<string, number>())
  const flushRequestedRef = useRef(new Set<string>())
  const sentenceSequenceRef = useRef(0)

  const refreshSettings = useCallback(async () => {
    const value = await api<TtsSettings>('/api/tts/settings')
    setSettings(value)
    return value
  }, [])

  useEffect(() => { void refreshSettings().catch(() => {}) }, [refreshSettings])
  useEffect(() => {
    const onSettings = (event: Event) => setSettings((event as CustomEvent<TtsSettings>).detail)
    window.addEventListener('siyi:tts-settings', onSettings)
    return () => window.removeEventListener('siyi:tts-settings', onSettings)
  }, [])

  const releaseAudio = useCallback(() => {
    playbackSettlementRef.current?.interrupt()
    playbackSettlementRef.current = null
    const audio = audioRef.current
    if (audio) {
      audio.pause()
      audio.src = ''
      audioRef.current = null
    }
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    objectUrlRef.current = null
  }, [])

  const interruptRendererTasks = useCallback(async (taskIds: Iterable<string>) => {
    const uniqueTaskIds = [...new Set(taskIds)]
    await Promise.all(uniqueTaskIds.map(async taskId => {
      // A renderer unmount must actively withdraw browser-owned playback,
      // rather than merely releasing the Voice Session handoff. The sidecar
      // then deletes non-cache WAVs for this task but leaves other tasks and
      // every shared cache entry untouched.
      await api('/api/tts/interrupt', {
        method: 'POST',
        body: JSON.stringify({ task_id: taskId }),
      }).catch(() => {})
      await api('/api/voice/tts-dispatch-finished', {
        method: 'POST',
        body: JSON.stringify({ task_id: taskId }),
      }).catch(() => {})
    }))
  }, [])

  useEffect(() => () => {
    generationRef.current += 1
    releaseAudio()
    const taskIds = new Set([
      ...buffersRef.current.keys(),
      ...pendingDispatchesRef.current.keys(),
      ...flushRequestedRef.current,
    ])
    buffersRef.current.clear()
    pendingDispatchesRef.current.clear()
    flushRequestedRef.current.clear()
    // If the WebView exits cleanly this reaches the sidecar immediately. A
    // hard renderer/process failure is still bounded by the server-side TTL
    // reaper; it never depends on another TTS request or app restart.
    void interruptRendererTasks(taskIds)
  }, [interruptRendererTasks, releaseAudio])

  const maybeFinishDispatch = useCallback((taskId: string) => {
    if (!flushRequestedRef.current.has(taskId) || (pendingDispatchesRef.current.get(taskId) || 0) > 0) return
    flushRequestedRef.current.delete(taskId)
    void api('/api/voice/tts-dispatch-finished', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    }).catch(() => {})
  }, [])

  const playSentence = useCallback(async (taskId: string, messageId: string, text: string, generation: number) => {
    if (!text.trim() || generation !== generationRef.current) return
    let requestId: string | null = null
    let audio: HTMLAudioElement | null = null
    let terminalReported = false
    try {
    const result = await api<SpeakResult>('/api/tts/speak', {
      method: 'POST',
      body: JSON.stringify({
        task_id: taskId,
        message_id: messageId,
        text,
        // Never put spoken text in correlation metadata. The sidecar hashes
        // this key again before persistence as a second trust-boundary guard.
        idempotency_key: `${taskId}:${messageId}:${++sentenceSequenceRef.current}`,
      }),
    })
    requestId = result.request_id
    if (!result.audio_url) throw new Error('TTS did not return playable audio')
    // An interrupt can arrive while the sidecar is synthesising. Do not
    // leave the just-created task-scoped WAV queued after that race.
    if (generation !== generationRef.current) throw new Error('TTS playback was interrupted')
    const response = await apiFetch(result.audio_url)
    if (!response.ok) throw new Error(`TTS 音频读取失败：HTTP ${response.status}`)
    const audioBlob = await response.blob()
    if (generation !== generationRef.current) throw new Error('TTS playback was interrupted')
    const objectUrl = URL.createObjectURL(audioBlob)
    objectUrlRef.current = objectUrl
    const playingAudio = new Audio(objectUrl)
    audio = playingAudio
    playingAudio.volume = Math.max(0, Math.min(1, settings?.volume ?? 1))
    audioRef.current = playingAudio
    try {
      await api(`/api/tts/playback/${requestId}/start`, { method: 'POST' })
      if (generation !== generationRef.current || audioRef.current !== playingAudio) {
        throw new Error('TTS playback was interrupted')
      }
      const settlement = createPlaybackSettlement()
      playbackSettlementRef.current = settlement
      playingAudio.onended = settlement.complete
      playingAudio.onerror = () => settlement.fail(new Error('TTS 音频播放失败'))
      try {
        await waitForPlayback(settlement, () => playingAudio.play())
      } finally {
        playingAudio.onended = null
        playingAudio.onerror = null
        if (playbackSettlementRef.current === settlement) playbackSettlementRef.current = null
      }
      await api(`/api/tts/playback/${requestId}/complete`, { method: 'POST' })
    } catch (error) {
      await api(`/api/tts/playback/${requestId}/complete?failed=true`, { method: 'POST' }).catch(() => {})
      terminalReported = true
      if (generation === generationRef.current) throw error
    } finally {
      if (audioRef.current === audio) releaseAudio()
    }
    } catch (error) {
      if (!terminalReported) {
        if (requestId) {
          // This covers fetch/decoder/start failures as well as Audio errors;
          // otherwise the sidecar would retain a queued non-cache WAV until a
          // later request happened to clean it up.
          await api(`/api/tts/playback/${requestId}/complete?failed=true`, { method: 'POST' }).catch(() => {})
        } else {
          // The response may have been lost after the sidecar created the
          // queue item. Scope recovery to this task, never a global queue.
          await api('/api/tts/interrupt', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId }),
          }).catch(() => {})
        }
      }
      if (generation === generationRef.current) throw error
    } finally {
      if (audio && audioRef.current === audio) releaseAudio()
    }
  }, [releaseAudio, settings])

  const enqueue = useCallback((taskId: string, messageId: string, text: string) => {
    const generation = generationRef.current
    pendingDispatchesRef.current.set(taskId, (pendingDispatchesRef.current.get(taskId) || 0) + 1)
    sequenceRef.current = sequenceRef.current
      .catch(() => {})
      .then(() => playSentence(taskId, messageId, text, generation))
      .finally(() => {
        const remaining = (pendingDispatchesRef.current.get(taskId) || 1) - 1
        if (remaining > 0) pendingDispatchesRef.current.set(taskId, remaining)
        else pendingDispatchesRef.current.delete(taskId)
        maybeFinishDispatch(taskId)
      })
  }, [maybeFinishDispatch, playSentence])

  const feed = useCallback((taskId: string, messageId: string, delta: string) => {
    if (!settings?.enabled || settings.playback_mode !== 'AUTO') return
    const buffer = buffersRef.current.get(taskId) || { text: '', inCodeFence: false, scan: 0 }
    for (const sentence of splitStreaming(buffer, delta)) enqueue(taskId, messageId, sentence)
    buffersRef.current.set(taskId, buffer)
  }, [enqueue, settings])

  const flush = useCallback((taskId: string, messageId: string) => {
    const state = buffersRef.current.get(taskId)
    const remaining = state && !state.inCodeFence ? state.text.trim() : ''
    buffersRef.current.delete(taskId)
    if (remaining && settings?.enabled && settings.playback_mode === 'AUTO') enqueue(taskId, messageId, remaining)
    // Always settle the server reservation, even if settings changed while a
    // task was streaming or no speakable sentence was emitted.
    flushRequestedRef.current.add(taskId)
    maybeFinishDispatch(taskId)
  }, [enqueue, maybeFinishDispatch, settings])

  const interrupt = useCallback(async (taskId?: string | null) => {
    generationRef.current += 1
    const taskIds = taskId
      ? new Set([taskId])
      : new Set([...buffersRef.current.keys(), ...pendingDispatchesRef.current.keys(), ...flushRequestedRef.current])
    buffersRef.current.clear()
    for (const id of taskIds) {
      pendingDispatchesRef.current.delete(id)
      flushRequestedRef.current.delete(id)
    }
    // The local-model panel has an explicit test player as well.  Make the
    // one voice-interrupt policy reach that player too.
    window.dispatchEvent(new Event('siyi:tts-interrupt'))
    releaseAudio()
    sequenceRef.current = Promise.resolve()
    // Voice capture waits for this promise before opening the microphone.
    // Propagate a server-side interrupt failure so recording fails closed
    // instead of overlapping audio playback with an active capture.
    await api('/api/tts/interrupt', { method: 'POST', body: JSON.stringify({ task_id: taskId || null }) })
    await Promise.all([...taskIds].map(id => api('/api/voice/tts-dispatch-finished', {
      method: 'POST',
      body: JSON.stringify({ task_id: id }),
    }).catch(() => {})))
  }, [releaseAudio])

  return { settings, refreshSettings, feed, flush, interrupt }
}
