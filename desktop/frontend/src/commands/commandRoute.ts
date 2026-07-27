export function isLocalCommandInput(content: string, existingTaskId?: string): boolean {
  return !existingTaskId && content.trimStart().startsWith('/')
}

export function composerRoute(content: string, options: { existingTaskId?: string; busy: boolean; steer?: boolean }): 'ignore' | 'command' | 'resume' | 'queue' | 'steer' | 'task' {
  if (!content.trim()) return 'ignore'
  if (isLocalCommandInput(content, options.existingTaskId)) return 'command'
  if (options.existingTaskId) return 'resume'
  if (options.steer && options.busy) return 'steer'
  if (options.busy) return 'queue'
  return 'task'
}
