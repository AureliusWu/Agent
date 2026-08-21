export interface PendingVoiceMediaStream {
  getTracks(): Array<{ stop(): void }>
}

export interface PendingVoiceAudioContext {
  resume(): Promise<void>
  close(): Promise<void>
}

export interface VoiceCaptureSetupToken {
  readonly generation: number
  readonly operation: number
  readonly voiceSessionId: string
}

interface PendingVoiceCaptureSetup {
  token: VoiceCaptureSetupToken
  stream: PendingVoiceMediaStream | null
  audioContext: PendingVoiceAudioContext | null
}

function stopAllTracks(stream: PendingVoiceMediaStream): void {
  for (const track of stream.getTracks()) {
    try { track.stop() } catch { /* continue releasing every owned track */ }
  }
}

function closeAudioContext(audioContext: PendingVoiceAudioContext): void {
  try { void audioContext.close().catch(() => undefined) } catch { /* already closed or invalid */ }
}

/**
 * Owns microphone resources during the gap before a full CaptureResources
 * object can be constructed. A late getUserMedia/resume result is released
 * immediately when its operation or Voice Session has lost ownership.
 */
export class VoiceCaptureSetupFence {
  private generation = 0
  private active: PendingVoiceCaptureSetup | null = null

  begin(operation: number, voiceSessionId: string): VoiceCaptureSetupToken {
    this.release()
    const token = { generation: ++this.generation, operation, voiceSessionId }
    this.active = { token, stream: null, audioContext: null }
    return token
  }

  owns(token: VoiceCaptureSetupToken, operation: number, voiceSessionId: string | null): boolean {
    return this.active?.token === token && token.operation === operation && token.voiceSessionId === voiceSessionId
  }

  registerStream(token: VoiceCaptureSetupToken, stream: PendingVoiceMediaStream): boolean {
    if (this.active?.token !== token) {
      stopAllTracks(stream)
      return false
    }
    this.active.stream = stream
    return true
  }

  registerAudioContext(token: VoiceCaptureSetupToken, audioContext: PendingVoiceAudioContext): boolean {
    if (this.active?.token !== token) {
      closeAudioContext(audioContext)
      return false
    }
    this.active.audioContext = audioContext
    return true
  }

  complete(token: VoiceCaptureSetupToken): boolean {
    if (this.active?.token !== token) return false
    this.active = null
    return true
  }

  release(token?: VoiceCaptureSetupToken): boolean {
    if (!this.active || (token && this.active.token !== token)) return false
    const owned = this.active
    this.active = null
    if (owned.stream) stopAllTracks(owned.stream)
    if (owned.audioContext) closeAudioContext(owned.audioContext)
    return true
  }

  hasOwner(): boolean {
    return this.active !== null
  }
}

export async function resumeAudioContextWithTimeout(
  audioContext: PendingVoiceAudioContext,
  timeoutMs: number,
): Promise<void> {
  let timeout: ReturnType<typeof setTimeout> | null = null
  try {
    await Promise.race([
      audioContext.resume(),
      new Promise<never>((_, reject) => {
        timeout = setTimeout(() => reject(new DOMException('音频设备启动超时', 'AbortError')), timeoutMs)
      }),
    ])
  } finally {
    if (timeout !== null) clearTimeout(timeout)
  }
}
