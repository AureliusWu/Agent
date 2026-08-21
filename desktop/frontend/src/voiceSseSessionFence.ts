export interface VoiceSseStreamToken {
  readonly generation: number
  readonly voiceSessionId: string
}

export interface VoiceSseDisconnectContext {
  /** Browser input is still live and must be released without waiting to reconnect. */
  inputActive: boolean
  /** getUserMedia/session creation is in flight and must not later acquire input. */
  startPending: boolean
  /** Non-capture phases may make a bounded reconnect attempt before cancellation. */
  reconnectLimitReached: boolean
}

/**
 * Keeps an SSE stream's terminal state bound to the exact Voice Session that
 * owns it. A delayed callback from an aborted stream cannot close or mark a
 * later session as terminal, even if it still has buffered events to deliver.
 */
export class VoiceSseSessionFence {
  private generation = 0
  private active: VoiceSseStreamToken | null = null
  private terminal: VoiceSseStreamToken | null = null

  begin(voiceSessionId: string): VoiceSseStreamToken {
    const token = { generation: ++this.generation, voiceSessionId }
    this.active = token
    this.terminal = null
    return token
  }

  owns(token: VoiceSseStreamToken, activeVoiceSessionId: string | null): boolean {
    return this.active === token && activeVoiceSessionId === token.voiceSessionId
  }

  markTerminal(token: VoiceSseStreamToken, activeVoiceSessionId: string | null): boolean {
    if (!this.owns(token, activeVoiceSessionId)) return false
    this.terminal = token
    return true
  }

  isTerminal(token: VoiceSseStreamToken): boolean {
    return this.terminal === token
  }

  activeOwner(): VoiceSseStreamToken | null {
    return this.active
  }

  /** Clears only the current owner; a stale stream can never clear a new one. */
  clear(token: VoiceSseStreamToken): boolean {
    if (this.active !== token) return false
    this.active = null
    if (this.terminal === token) this.terminal = null
    return true
  }

  /**
   * Input capture is privacy-sensitive: its SSE authority disappearing must
   * stop capture immediately. Once input was released, a bounded reconnect
   * remains safe for the longer transcription/Agent/TTS lifecycle.
   */
  shouldFailClosed(
    token: VoiceSseStreamToken,
    activeVoiceSessionId: string | null,
    context: VoiceSseDisconnectContext,
  ): boolean {
    if (!this.owns(token, activeVoiceSessionId) || this.isTerminal(token)) return false
    return context.inputActive || context.startPending || context.reconnectLimitReached
  }
}
