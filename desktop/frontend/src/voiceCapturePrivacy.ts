export interface VoiceCapturePrivacyState {
  capturePresent: boolean
  inputReleased: boolean
  setupPending: boolean
  startPending: boolean
}

/**
 * Decide whether losing window visibility/focus must cancel a voice session.
 *
 * Logical flags such as `cancelled` or `released` are deliberately excluded:
 * privacy is fail-closed until the microphone input has been physically
 * released. Once every input track is stopped, local WAV conversion and STT
 * may finish without retaining microphone access.
 */
export function shouldCancelVoiceForPrivacy(state: VoiceCapturePrivacyState): boolean {
  return (
    (state.capturePresent && !state.inputReleased) ||
    state.setupPending ||
    state.startPending
  )
}
