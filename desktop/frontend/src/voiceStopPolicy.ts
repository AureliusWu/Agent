export interface VoiceTaskStopAuthority {
  coveredByVoice: boolean
  status: string | null
  settled: boolean
}

const FINAL_TASK_STATUSES = new Set([
  'completed',
  'partially_completed',
  'failed',
  'cancelled',
  'blocked',
])

export function isFinalTaskStatus(status: string | null): boolean {
  return status !== null && FINAL_TASK_STATUSES.has(status)
}

/**
 * Selects the single cancellation authority for the current Agent task.
 * An exact task entry in the global Voice stop aggregate means the backend has
 * already cancelled that bound task and its queue items. Only an unlisted task
 * is an ordinary, non-Voice task that still needs `/api/tasks/{id}/cancel`.
 */
export function resolveVoiceTaskStopAuthority(
  taskId: string | null,
  response: unknown,
): VoiceTaskStopAuthority {
  if (!taskId || !response || typeof response !== 'object') {
    return { coveredByVoice: false, status: null, settled: false }
  }
  const agentTasks = (response as { agent_tasks?: unknown }).agent_tasks
  if (!Array.isArray(agentTasks)) {
    return { coveredByVoice: false, status: null, settled: false }
  }
  const aggregate = agentTasks.find(item => (
    item !== null
    && typeof item === 'object'
    && (item as { task_id?: unknown }).task_id === taskId
  )) as Record<string, unknown> | undefined
  if (!aggregate) {
    return { coveredByVoice: false, status: null, settled: false }
  }

  const status = typeof aggregate.status === 'string' ? aggregate.status : null
  const queueItemsActive = typeof aggregate.queue_items_active === 'number'
    ? aggregate.queue_items_active
    : null
  const explicitlySettled = aggregate.settled === true
    && aggregate.active !== true
    && queueItemsActive === 0
  return {
    coveredByVoice: true,
    status,
    // The aggregate owns queue + runtime convergence.  A durable task may
    // reach a final row while a claimed queue item, TTS process, or Voice
    // resource is still active; never let that row overrule settled=false.
    settled: explicitlySettled,
  }
}

export function isVoiceStopAggregateSettled(response: unknown): boolean {
  if (!response || typeof response !== 'object') return false
  const aggregate = response as {
    status?: unknown
    sessions?: unknown
    agent_tasks?: unknown
    tts?: unknown
  }
  if (aggregate.status !== 'CANCELLED') return false
  if (!Array.isArray(aggregate.sessions) || !Array.isArray(aggregate.agent_tasks)) return false
  const sessionsSettled = aggregate.sessions.every(item => (
    item !== null
    && typeof item === 'object'
    && !['REQUESTING_PERMISSION', 'RECORDING', 'AUDIO_PROCESSING', 'TRANSCRIBING', 'REVIEWING', 'QUEUED_FOR_AGENT', 'AGENT_RUNNING', 'TTS_PLAYING', 'CANCEL_REQUESTED']
      .includes(String((item as { state?: unknown }).state || ''))
  ))
  const tasksSettled = aggregate.agent_tasks.every(item => (
    item !== null
    && typeof item === 'object'
    && (item as { settled?: unknown }).settled === true
    && (item as { active?: unknown }).active !== true
    && (item as { queue_items_active?: unknown }).queue_items_active === 0
  ))
  const tts = aggregate.tts
  let ttsSettled = tts === undefined
  if (tts !== null && typeof tts === 'object') {
    const status = String((tts as { status?: unknown }).status || '')
    const unresolved = (tts as { unresolved_requests?: unknown }).unresolved_requests
    ttsSettled = (
      ['CANCELLED', 'NOT_APPLICABLE', 'ALREADY_TERMINAL'].includes(status)
      && (tts as { settled?: unknown }).settled !== false
      && (unresolved === undefined || (Array.isArray(unresolved) && unresolved.length === 0))
    )
  }
  return sessionsSettled && tasksSettled && ttsSettled
}
