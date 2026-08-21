export function isLocalCommandInput(content: string, existingTaskId?: string): boolean {
  return !existingTaskId && content.trimStart().startsWith('/')
}

export function composerRoute(content: string, options: { existingTaskId?: string; busy: boolean; steer?: boolean; voiceSessionId?: string | null }): 'ignore' | 'command' | 'resume' | 'queue' | 'steer' | 'task' {
  if (!content.trim()) return 'ignore'
  // A transcription is user content bound to a server Voice Session. Even if
  // it resembles /stop or /clear, it must enter the normal task API rather
  // than crossing the local composer-command boundary.
  if (!options.voiceSessionId && isLocalCommandInput(content, options.existingTaskId)) return 'command'
  if (options.existingTaskId) return 'resume'
  if (options.steer && options.busy) return 'steer'
  if (options.busy) return 'queue'
  return 'task'
}
