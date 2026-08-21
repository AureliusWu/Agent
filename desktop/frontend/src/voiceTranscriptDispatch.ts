/**
 * Starts voice auto-send without coupling microphone cleanup to the eventual
 * Agent terminal state. Submission errors remain observable by the caller.
 */
export function dispatchVoiceTranscript(
  text: string,
  send: (content: string) => Promise<void>,
  onError: (error: unknown) => void,
): void {
  void send(text).catch(onError)
}
