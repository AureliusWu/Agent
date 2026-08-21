import { api, apiFetch, ApiError } from './api'

export type VoiceSessionState =
  | 'REQUESTING_PERMISSION'
  | 'RECORDING'
  | 'AUDIO_PROCESSING'
  | 'TRANSCRIBING'
  | 'REVIEWING'
  | 'QUEUED_FOR_AGENT'
  | 'AGENT_RUNNING'
  | 'TTS_PLAYING'
  | 'CANCEL_REQUESTED'
  | 'CANCELLED'
  | 'FAILED'
  | 'COMPLETED'

export interface VoiceSession {
  voice_session_id: string
  conversation_id: number
  state: VoiceSessionState
  auto_send: boolean
  microphone_device_id?: string | null
  audio_duration_ms?: number | null
  error_code?: string | null
}

export interface VoiceTranscription {
  request_id: string
  provider: string
  model: string
  language: string
  text: string
  duration_ms: number
  transcription_ms: number
  confidence: number | null
  reliable: boolean
  reliability_reason: string
}

export interface VoiceTranscriptionResponse {
  session: VoiceSession
  transcription: VoiceTranscription
}

export interface MicrophoneSettings {
  selected_device_id: string
  selected_device_label: string
  max_duration_ms: number
  min_duration_ms: number
  auto_send: boolean
  shortcut: string
}

/** Metadata-only state events; transcript and audio bytes never travel on this stream. */
export interface VoiceStreamEvent {
  id: number
  voice_session_id: string
  event: string
  payload: Record<string, unknown>
  created_at: string
}

export type MicrophoneSettingsUpdate = Partial<Pick<
  MicrophoneSettings,
  'selected_device_id' | 'selected_device_label' | 'max_duration_ms' | 'min_duration_ms' | 'auto_send' | 'shortcut'
>>

export async function createVoiceSession(conversationId: number, deviceId: string, autoSend: boolean): Promise<VoiceSession> {
  return api<VoiceSession>('/api/stt/sessions', {
    method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, device_id: deviceId, auto_send: autoSend }),
  })
}

export async function markVoiceRecordingStarted(voiceSessionId: string, deviceId: string): Promise<VoiceSession> {
  return api<VoiceSession>(`/api/stt/sessions/${encodeURIComponent(voiceSessionId)}/recording-started`, {
    method: 'POST',
    body: JSON.stringify({ device_id: deviceId }),
  })
}

/**
 * One quantized, metadata-only sample for the Voice Event timeline.  The
 * backend rate-limits this to one record per second; the live UI meter stays
 * local and does not send audio or waveform data.
 */
export async function reportVoiceRecordingLevel(voiceSessionId: string, level: number): Promise<VoiceSession> {
  return api<VoiceSession>(`/api/stt/sessions/${encodeURIComponent(voiceSessionId)}/recording-level`, {
    method: 'POST',
    body: JSON.stringify({ level: Math.max(0, Math.min(1, level)) }),
  })
}

export async function markVoicePermissionDenied(voiceSessionId: string): Promise<VoiceSession> {
  return api<VoiceSession>(`/api/stt/sessions/${encodeURIComponent(voiceSessionId)}/permission-denied`, { method: 'POST' })
}

export async function transcribeVoiceAudio(voiceSessionId: string, audio: Blob, signal?: AbortSignal): Promise<VoiceTranscriptionResponse> {
  const form = new FormData()
  form.append('voice_session_id', voiceSessionId)
  form.append('file', audio, 'voice-input.wav')
  return api<VoiceTranscriptionResponse>('/api/stt/transcribe', { method: 'POST', body: form, signal })
}

export async function cancelVoiceSession(voiceSessionId: string): Promise<VoiceSession> {
  return api<VoiceSession>(`/api/stt/sessions/${encodeURIComponent(voiceSessionId)}/cancel`, { method: 'POST' })
}

export async function getMicrophoneSettings(): Promise<MicrophoneSettings> {
  return api<MicrophoneSettings>('/api/stt/microphone/settings')
}

export async function updateMicrophoneSettings(change: MicrophoneSettingsUpdate): Promise<MicrophoneSettings> {
  return api<MicrophoneSettings>('/api/stt/microphone/settings', { method: 'PUT', body: JSON.stringify(change) })
}

/**
 * Consume the authenticated local SSE endpoint with the same fetch path used by
 * task events. EventSource cannot provide the desktop API token.
 */
export async function streamVoiceEvents(
  voiceSessionId: string,
  onEvent: (event: VoiceStreamEvent) => void | Promise<void>,
  signal: AbortSignal,
  afterId = 0,
): Promise<number> {
  const query = new URLSearchParams({ voice_session_id: voiceSessionId, after_id: String(afterId) })
  const response = await apiFetch(`/api/voice/events?${query.toString()}`, {
    signal,
    headers: { Accept: 'text/event-stream' },
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new ApiError(String(body.detail || `HTTP ${response.status}`), response.status, body.detail)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let cursor = afterId
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
      if (data) {
        const event = JSON.parse(data) as VoiceStreamEvent
        cursor = Math.max(cursor, event.id)
        await onEvent(event)
      }
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  return cursor
}
