import { useCallback, useEffect, useRef, useState } from 'react'
import { api, apiFetch } from '../api'

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

interface StreamBuffer { text: string; inCodeFence: boolean; scan: number }

function splitStreaming(state: StreamBuffer, delta: string): string[] {
  state.text += delta
  const sentences: string[] = []
  let cursor = 0
  let index = state.scan
  while (index < state.text.length) {
    if (state.text.startsWith('```', index) || state.text.startsWith('~~~', index)) {
      state.inCodeFence = !state.inCodeFence
      index += 3
      continue
    }
    if (!state.inCodeFence && /[。！？!?；;\n]/.test(state.text[index])) {
      const sentence = state.text.slice(cursor, index + 1).trim()
      if (sentence) sentences.push(sentence)
      cursor = index + 1
    }
    index += 1
  }
  state.text = state.text.slice(cursor)
  state.scan = Math.max(0, index - cursor)
  return sentences
}

export function useTtsPlayback() {
  const [settings, setSettings] = useState<TtsSettings | null>(null)
  const buffersRef = useRef(new Map<string, StreamBuffer>())
  const sequenceRef = useRef(Promise.resolve())
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const objectUrlRef = useRef<string | null>(null)
  const generationRef = useRef(0)

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
    const audio = audioRef.current
    if (audio) {
      audio.pause()
      audio.src = ''
      audioRef.current = null
    }
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current)
    objectUrlRef.current = null
  }, [])

  useEffect(() => () => {
    generationRef.current += 1
    releaseAudio()
  }, [releaseAudio])

  const playSentence = useCallback(async (taskId: string, messageId: string, text: string, generation: number) => {
    if (!text.trim() || generation !== generationRef.current) return
    const result = await api<SpeakResult>('/api/tts/speak', {
      method: 'POST',
      body: JSON.stringify({
        task_id: taskId,
        message_id: messageId,
        text,
        idempotency_key: `${taskId}:${messageId}:${text}`,
      }),
    })
    if (!result.audio_url || generation !== generationRef.current) return
    const response = await apiFetch(result.audio_url)
    if (!response.ok) throw new Error(`TTS 音频读取失败：HTTP ${response.status}`)
    const objectUrl = URL.createObjectURL(await response.blob())
    objectUrlRef.current = objectUrl
    const audio = new Audio(objectUrl)
    audioRef.current = audio
    await api(`/api/tts/playback/${result.request_id}/start`, { method: 'POST' })
    try {
      await audio.play()
      await new Promise<void>((resolve, reject) => {
        audio.onended = () => resolve()
        audio.onerror = () => reject(new Error('TTS 音频播放失败'))
      })
      await api(`/api/tts/playback/${result.request_id}/complete`, { method: 'POST' })
    } catch (error) {
      await api(`/api/tts/playback/${result.request_id}/complete?failed=true`, { method: 'POST' }).catch(() => {})
      if (generation === generationRef.current) throw error
    } finally {
      if (audioRef.current === audio) releaseAudio()
    }
  }, [releaseAudio])

  const enqueue = useCallback((taskId: string, messageId: string, text: string) => {
    const generation = generationRef.current
    sequenceRef.current = sequenceRef.current
      .catch(() => {})
      .then(() => playSentence(taskId, messageId, text, generation))
  }, [playSentence])

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
  }, [enqueue, settings])

  const interrupt = useCallback(async (taskId?: string | null) => {
    generationRef.current += 1
    buffersRef.current.clear()
    releaseAudio()
    sequenceRef.current = Promise.resolve()
    await api('/api/tts/interrupt', { method: 'POST', body: JSON.stringify({ task_id: taskId || null }) }).catch(() => {})
  }, [releaseAudio])

  return { settings, refreshSettings, feed, flush, interrupt }
}
