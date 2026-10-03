import { api } from '../api'

export interface TranscriptResponse {
  text: string
  provider_id: string
  language: string | null
  duration_ms: number | null
  confidence: number | null
}

export interface SpeechResponse {
  audio_base64: string
  mime_type: string
  provider_id: string
  duration_ms: number | null
}

function bytesToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}

function base64ToBlob(encoded: string, mimeType: string): Blob {
  const binary = atob(encoded)
  const bytes = new Uint8Array(binary.length)
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index)
  return new Blob([bytes], { type: mimeType })
}

export async function transcribeVoice(
  blob: Blob,
  options: { language?: string; providerId?: string } = {},
): Promise<TranscriptResponse> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  return api<TranscriptResponse>('/api/voice/transcribe', {
    method: 'POST',
    body: JSON.stringify({
      audio_base64: bytesToBase64(bytes),
      mime_type: blob.type || 'application/octet-stream',
      language: options.language || null,
      provider_id: options.providerId || null,
    }),
  })
}

export async function synthesizeVoice(
  text: string,
  options: { voice?: string; speed?: number; providerId?: string } = {},
): Promise<{ blob: Blob; meta: SpeechResponse }> {
  const meta = await api<SpeechResponse>('/api/voice/synthesize', {
    method: 'POST',
    body: JSON.stringify({
      text,
      voice: options.voice || null,
      speed: options.speed ?? 1,
      audio_format: 'wav',
      provider_id: options.providerId || null,
    }),
  })
  return { blob: base64ToBlob(meta.audio_base64, meta.mime_type), meta }
}
