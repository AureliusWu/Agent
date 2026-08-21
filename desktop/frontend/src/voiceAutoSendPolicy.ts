export const MIN_VOICE_AUTO_SEND_CONFIDENCE = 0.60

export interface VoiceAutoSendCandidate {
  text: string
  confidence?: number | null
  reliable?: boolean
  reliability_reason?: string
}

/** Fail closed unless the local STT provider explicitly marks this result reliable. */
export function shouldAutoSendVoiceTranscript(
  configured: boolean,
  candidate: VoiceAutoSendCandidate,
): boolean {
  const confidence = candidate.confidence
  return configured
    && candidate.text.trim().length > 0
    && candidate.reliable === true
    && candidate.reliability_reason === 'RELIABLE'
    && typeof confidence === 'number'
    && Number.isFinite(confidence)
    && confidence >= MIN_VOICE_AUTO_SEND_CONFIDENCE
}
