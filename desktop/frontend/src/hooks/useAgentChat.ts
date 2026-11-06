import { useEffect, useRef, useState } from 'react'
import { api, ApiError, streamTaskEvents } from '../api'
import { executeLocalCommand, loadCommandCatalog } from '../commands/commandRegistry'
import { composerRoute } from '../commands/commandRoute'
import { mergeReasoningSummaries, publicReasoningSummary } from '../reasoningEvents'
import { extractArtifactDownloads, mergeArtifactDownloads } from '../shared/artifactDownloads'
import { useTtsPlayback } from './useTtsPlayback'
import { useConversationRef, useConversationState } from './useConversationState'
import { ConversationScope } from '../shared/desktopReliability'
import { LatestRequest } from '../shared/latestRequest'
import type { TaskAttentionTarget } from '../shared/taskAttention'
import type { VoiceTranscriptInput } from './useVoiceCapture'
import { dispatchVoiceTranscript } from '../voiceTranscriptDispatch'
import { isFinalTaskStatus, isVoiceStopAggregateSettled, resolveVoiceTaskStopAuthority } from '../voiceStopPolicy'
import type { CommandDefinition, ContextStats, Conversation, ConversationQueueItem, Message, OrchestrationMode, PendingAction, ReasoningEffort, RecoverableTask, RuntimeEvent, TokenUsage, VerificationReport, View } from '../types'

const REASONING_EFFORT_KEY = 'agent_reasoning_effort'
const PREFERRED_MODEL_KEY = 'agent_preferred_model'
const NO_PENDING_ACTIONS: PendingAction[] = []
const EXECUTING_TASK_STATUSES = new Set(['planning', 'ready', 'running', 'waiting_tool', 'verifying', 'repairing', 'rolling_back', 'recovering', 'cancel_requested'])
const savedReasoningEffort = (): ReasoningEffort => {
  const value = localStorage.getItem(REASONING_EFFORT_KEY)
  return value === 'low' || value === 'medium' || value === 'high' ? value : 'auto'
}

interface ChatResult {
  content: string
  reasoning?: string
  pending_actions: PendingAction[]
  context?: ContextStats
  task_status: string
  task_id: string
  resumable?: boolean
  verification?: VerificationReport
  recovery?: { execution_id: string; tool: string; retry_requires_confirmation: boolean }
  usage?: TokenUsage
}

interface ResumeOptions {
  allowWorkspaceDrift?: boolean
  retryUncertain?: boolean
  checkpointSequence?: number
}

interface TaskSnapshot {
  id: string
  conversation_id?: number
  status: string
  result?: ChatResult
  result_event_id?: number
  event_cursor?: number
  queue_claimed?: boolean
}

function resumeCursor(snapshot: TaskSnapshot): number {
  const cursor = snapshot.event_cursor
  if (typeof cursor !== 'number' || !Number.isSafeInteger(cursor) || cursor <= 0) {
    throw new Error('核心未返回恢复游标，任务可能已继续。请刷新任务状态。')
  }
  return cursor
}

function attachableTask(tasks: TaskSnapshot[], queue: ConversationQueueItem[], conversationId: number, exclude?: string) {
  const owned = tasks.filter(task => task.conversation_id === conversationId && task.id !== exclude)
  // The active list is newest-first for display. Controls must attach to
  // actual execution first, then to the persisted queue order, not recency.
  return owned.find(task => EXECUTING_TASK_STATUSES.has(task.status))
    ?? owned.find(task => task.queue_claimed === true)
    ?? queue.filter(item => item.conversation_id === conversationId && (item.kind === 'submit' || item.kind === 'resume'))
      .map(item => owned.find(task => task.id === item.task_id)).find(task => task !== undefined)
    ?? owned[0]
}

function attachmentCursor(snapshot: TaskSnapshot): number {
  // Zero is supplied by the backend only for legacy tasks without an
  // admission event; a missing cursor must not replay old attempts.
  return snapshot.event_cursor === 0 ? 0 : resumeCursor(snapshot)
}

function firstOwnedQueueTask(queue: ConversationQueueItem[], conversationId: number, exclude?: string) {
  return queue.find(item => item.conversation_id === conversationId &&
    item.task_id && item.task_id !== exclude && (item.kind === 'submit' || item.kind === 'resume'))?.task_id
}

async function queueTaskReadback(queue: ConversationQueueItem[], conversationId: number, exclude?: string) {
  const taskId = firstOwnedQueueTask(queue, conversationId, exclude)
  if (!taskId) return
  // The worker can finish between queue and active-list reads. Read back the
  // exact durable queued identity rather than opening an unscoped event stream.
  const task = await api<TaskSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}`, { cache: 'no-store' })
    .catch(caught => {
      if (caught instanceof ApiError && caught.status === 404) return null
      throw caught
    })
  return task?.id === taskId && task.conversation_id === conversationId ? task : undefined
}

export type StopState = 'idle' | 'stopping' | 'stopped' | 'failed'

export function useAgentChat(active: Conversation | null, refreshConversations: () => void, navigate: (view: View, query?: string) => void, orchestrationMode: OrchestrationMode = 'single', attentionTarget: TaskAttentionTarget | null = null) {
  const scopeRef = useRef(new ConversationScope())
  const scope = scopeRef.current
  const owner = scope.select(active?.id ?? null)
  const [messages, setMessages] = useConversationState<Message[]>(scope, [])
  const [input, setInput] = useConversationState(scope, '')
  const [busy, setBusy] = useConversationState(scope, false)
  const [error, setError] = useConversationState(scope, '')
  const [pendingReview, setPendingReview] = useConversationState<{ taskId: string; actions: PendingAction[] } | null>(scope, null)
  const [context, setContext] = useConversationState<ContextStats | null>(scope, null)
  const [runningTaskId, setRunningTaskId] = useConversationState<string | null>(scope, null)
  const [stopState, setStopState] = useConversationState<StopState>(scope, 'idle')
  const [verification, setVerification] = useConversationState<VerificationReport | null>(scope, null)
  const [recoverableTasks, setRecoverableTasks] = useConversationState<RecoverableTask[]>(scope, [])
  const [checkpoint, setCheckpoint] = useConversationState<{ taskId: string; head: number; sequence: number } | null>(scope, null)
  const [driftTaskId, setDriftTaskId] = useConversationState<string | null>(scope, null)
  const [uncertainTaskId, setUncertainTaskId] = useConversationState<string | null>(scope, null)
  const [reviewCheck, setReviewCheck] = useConversationState<{ target: TaskAttentionTarget; ok: boolean } | null>(scope, null)
  const target = attentionTarget?.conversationId === active?.id ? attentionTarget : null
  const targetRef = useRef(target)
  targetRef.current = target
  const reviewed = reviewCheck?.target === target && reviewCheck.ok
  const recoverable = target ? (reviewed ? recoverableTasks.find(item => item.id === target.taskId) ?? null : null) : (recoverableTasks[0] ?? null)
  const pendingVisible = Boolean(pendingReview && (!target || recoverable?.id === pendingReview.taskId))
  const pending = pendingVisible ? pendingReview!.actions : NO_PENDING_ACTIONS
  const reviewStatus: 'checking' | 'unavailable' | 'missing' | null = !target ? null : reviewCheck?.target !== target ? 'checking' : !reviewCheck.ok ? 'unavailable' : !recoverable ? 'missing' : null
  const head = recoverable?.checkpoints[0]?.sequence
  const selectedCheckpoint = checkpoint && checkpoint.taskId === recoverable?.id && checkpoint.head === head ? checkpoint.sequence : head ?? null
  const workspaceDrift = Boolean(recoverable && driftTaskId === recoverable.id)
  const uncertainOperation = Boolean(recoverable && uncertainTaskId === recoverable.id)
  const setSelectedCheckpoint = (sequence: number) => {
    if (recoverable && head !== undefined && recoverable.checkpoints.some(item => item.sequence === sequence)) {
      setCheckpoint({ taskId: recoverable.id, head, sequence })
    }
  }
  const [reasoningEffort, setReasoningEffortState] = useState<ReasoningEffort>(savedReasoningEffort)
  const [preferredModel, setPreferredModelState] = useState(() => localStorage.getItem(PREFERRED_MODEL_KEY) || '')
  const [usage, setUsage] = useConversationState<TokenUsage | null>(scope, null)
  const [queued, setQueued] = useConversationState<ConversationQueueItem[]>(scope, [])
  const [runtimeEvents, setRuntimeEvents] = useConversationState<RuntimeEvent[]>(scope, [])
  const [commands, setCommands] = useState<CommandDefinition[]>([])
  const [lastBoundVoiceSessionId, setLastBoundVoiceSessionId] = useConversationState<string | null>(scope, null)
  const controllerRef = useConversationRef<AbortController | null>(scope, null)
  const runningTaskRef = useConversationRef<string | null>(scope, null)
  const sessionApprovalTokensRef = useConversationRef<string[]>(scope, [])
  const voiceSessionRef = useConversationRef<{ id: string } | null>(scope, null)
  const conversationLoadRef = useConversationRef(scope, new LatestRequest())
  const recoveryLoadRef = useConversationRef(scope, new LatestRequest())
  const endRef = useRef<HTMLDivElement>(null)
  const tts = useTtsPlayback()

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, pending, recoverable])
  useEffect(() => () => controllerRef.current?.abort(), [controllerRef])
  // The global catalog is loaded once; any error setter is fenced to the loading conversation.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadCommandCatalog().then(setCommands).catch(caught => setError((caught as Error).message)) }, [])
  useEffect(() => {
    const loadGate = conversationLoadRef.current
    resetConversation()
    if (active) void loadConversation(active).catch(caught => setError((caught as Error).message))
    const recoveryGate = recoveryLoadRef.current
    return () => { loadGate.begin(); recoveryGate.begin() }
    // Ownership changes synchronously on render; this effect hydrates that conversation only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active?.id])
  useEffect(() => {
    if (target) void refreshRecoverable(target.conversationId).catch(() => undefined)
    // The current target is an explicit selection, never an inferred task.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target])

  async function runLocalCommand(content: string): Promise<boolean> {
    if (!content.trim().startsWith('/')) return false
    setInput('')
    setError('')
    try {
      const catalog = commands.length ? commands : await loadCommandCatalog()
      if (!scope.accepts(owner)) return true
      if (!commands.length) setCommands(catalog)
      return await executeLocalCommand({
        text: content,
        conversationId: active?.id || null,
        runningTaskId: runningTaskRef.current,
        hasWorkspace: Boolean(active?.workspace),
        waitingConfirmation: Boolean(pending.length),
        recovering: Boolean(recoverable),
        catalog,
        append: message => setMessages(old => [...old, message]),
        clearMessages: () => setMessages([]),
        updateContext: setContext,
        stopTask: () => stopTaskWithPolicy(true),
        navigate: (view, query) => { if (scope.accepts(owner)) navigate(view, query) },
      })
    } catch (caught) {
      setError((caught as Error).message)
      return true
    }
  }

  async function refreshRecoverable(conversationId = active?.id) {
    if (!conversationId || owner.conversationId !== conversationId || !scope.accepts(owner)) return
    const gate = recoveryLoadRef.current
    const request = gate.begin()
    const selected = targetRef.current
    const ownsRequest = () => scope.accepts(owner) && gate.isLatest(request) && targetRef.current === selected
    try {
      const tasks = await api<RecoverableTask[]>(`/api/tasks/recoverable?conversation_id=${conversationId}`)
      if (ownsRequest()) {
        setRecoverableTasks(tasks.filter(task => task.conversation_id === conversationId))
        if (selected) setReviewCheck({ target: selected, ok: true })
      }
    } catch (caught) {
      if (ownsRequest() && selected) setReviewCheck({ target: selected, ok: false })
      throw caught
    }
  }

  async function refreshQueue(conversationId = active?.id) {
    if (!conversationId) {
      setQueued([])
      return []
    }
    const items = await api<ConversationQueueItem[]>(`/api/conversations/${conversationId}/queue`)
    if (scope.accepts(owner)) setQueued(items)
    return items
  }

  async function loadConversation(item: Conversation) {
    if (!scope.accepts(owner) || item.id !== owner.conversationId) return
    const request = conversationLoadRef.current.begin()
    const ownsLoad = () => scope.accepts(owner) && conversationLoadRef.current.isLatest(request)
    controllerRef.current?.abort()
    await tts.interrupt(runningTaskRef.current)
    if (!ownsLoad()) return
    window.dispatchEvent(new Event('siyi:voice-stop'))
    await api('/api/voice/stop', { method: 'POST', body: '{}' }).catch(() => undefined)
    if (!ownsLoad()) return
    voiceSessionRef.current = null
    setLastBoundVoiceSessionId(null)
    sessionApprovalTokensRef.current = []
    const [loadedMessages, stats, activeTasks, queueItems] = await Promise.all([
      api<Message[]>(`/api/conversations/${item.id}/messages`),
      api<ContextStats>(`/api/conversations/${item.id}/context`),
      api<TaskSnapshot[]>(`/api/tasks?conversation_id=${item.id}&active=true`),
      api<ConversationQueueItem[]>(`/api/conversations/${item.id}/queue`),
      refreshRecoverable(item.id),
    ])
    if (!ownsLoad() || runningTaskRef.current) return
    setMessages(loadedMessages)
    setContext(stats)
    setPendingReview(null)
    setVerification(null)
    setUsage(null)
    setQueued(queueItems)
    setCheckpoint(null)
    setDriftTaskId(null)
    setUncertainTaskId(null)
    const task = attachableTask(activeTasks, queueItems, item.id) ?? await queueTaskReadback(queueItems, item.id)
    if (task && ownsLoad() && !runningTaskRef.current) {
      const cursor = attachmentCursor(task)
      const controller = new AbortController()
      controllerRef.current = controller
      void attachTask(task.id, controller, cursor)
    }
  }

  function resetConversation() {
    conversationLoadRef.current.begin()
    recoveryLoadRef.current.begin()
    controllerRef.current?.abort()
    void tts.interrupt(runningTaskRef.current)
    window.dispatchEvent(new Event('siyi:voice-stop'))
    void api('/api/voice/stop', { method: 'POST', body: '{}' }).catch(() => undefined)
    setMessages([])
    setPendingReview(null)
    setVerification(null)
    setRecoverableTasks([])
    setReviewCheck(null)
    setCheckpoint(null)
    setDriftTaskId(null)
    setUncertainTaskId(null)
    setContext(null)
    setInput('')
    setUsage(null)
    setQueued([])
    setRuntimeEvents([])
    setBusy(false)
    setError('')
    setStopState('idle')
    setRunningTaskId(null)
    runningTaskRef.current = null
    sessionApprovalTokensRef.current = []
    voiceSessionRef.current = null
    setLastBoundVoiceSessionId(null)
  }

  async function applyResult(result: ChatResult, streamed = false) {
    const resultArtifacts = extractArtifactDownloads(result)
    if (result.task_status !== 'cancelled') {
      setMessages(old => {
        const index = old.findIndex(item => item.role === 'assistant' && item.task_id === result.task_id)
        if (streamed && index >= 0) {
          const next = [...old]
          next[index] = {
            ...next[index],
            role: 'assistant',
            content: result.content,
            reasoning: result.reasoning || next[index].reasoning,
            artifacts: mergeArtifactDownloads(next[index].artifacts || [], resultArtifacts),
          }
          return next
        }
        return [...old, {
          role: 'assistant',
          content: result.content,
          reasoning: result.reasoning,
          artifacts: resultArtifacts,
          task_id: result.task_id,
          created_at: new Date().toISOString(),
        }]
      })
    }
    const actions = (result.pending_actions || []).filter(action => action.approval_key && action.approval_key !== '***REDACTED***')
    setPendingReview(actions.length ? { taskId: result.task_id, actions } : null)
    setVerification(result.verification || null)
    if (result.usage) setUsage(result.usage)
    setUncertainTaskId(result.recovery?.retry_requires_confirmation ? result.task_id : null)
    setDriftTaskId(null)
    if (result.context) setContext(result.context)
    await refreshRecoverable()
    refreshConversations()
    window.setTimeout(refreshConversations, 750)
    window.setTimeout(refreshConversations, 2500)
  }

  async function attachTask(taskId: string, controller: AbortController, afterId = 0) {
    if (!scope.accepts(owner) || controller.signal.aborted) return
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    let streamed = false
    let finalResult: ChatResult | undefined
    let resumeRejected = false
    let cursor = afterId
    const ownsStream = () => scope.accepts(owner) && !controller.signal.aborted &&
      controllerRef.current === controller && runningTaskRef.current === taskId
    const currentResult = (snapshot: TaskSnapshot) => {
      if (snapshot.id !== taskId || snapshot.result?.task_id !== taskId) return
      if (afterId > 0 && (snapshot.event_cursor !== afterId || (snapshot.result_event_id ?? 0) <= afterId)) return
      return snapshot.result
    }
    let reconnects = 0
    try {
      while (!finalResult && !resumeRejected) {
        try {
          await streamTaskEvents(taskId, event => {
            if (!ownsStream() || event.task_id !== taskId || event.id <= cursor) return
            cursor = event.id
            if (afterId > 0 && event.event === 'task.interrupted' && event.payload.resume_rejected === true) {
              resumeRejected = true
              if (event.payload.code === 'workspace_drift') setDriftTaskId(taskId)
              setError(String(event.payload.error || '核心拒绝恢复；原任务状态未更改。'))
              return
            }
            const artifactDownloads = extractArtifactDownloads(event.payload, String(event.payload.tool || ''))
            if (artifactDownloads.length > 0) {
              streamed = true
              setMessages(old => {
                const index = old.findIndex(item => item.role === 'assistant' && item.task_id === taskId)
                if (index < 0) {
                  return [...old, {
                    role: 'assistant',
                    content: '',
                    artifacts: artifactDownloads,
                    task_id: taskId,
                    created_at: new Date().toISOString(),
                  }]
                }
                const next = [...old]
                next[index] = {
                  ...next[index],
                  artifacts: mergeArtifactDownloads(next[index].artifacts || [], artifactDownloads),
                }
                return next
              })
            }
            if (event.event.startsWith('search.') || event.event.startsWith('execution.segment.') || event.event.startsWith('context.compaction.') || event.event.startsWith('tool.scheduler.')) {
              setRuntimeEvents(old => [...old.slice(-79), event as RuntimeEvent])
            }
            if (event.event === 'model.delta') {
              const delta = String(event.payload.delta || '')
              if (!delta) return
              streamed = true
              tts.feed(taskId, taskId, delta)
              setMessages(old => {
                const index = old.findIndex(item => item.role === 'assistant' && item.task_id === taskId)
                if (index < 0) return [...old, { role: 'assistant', content: delta, task_id: taskId, created_at: new Date().toISOString() }]
                const next = [...old]
                next[index] = { ...next[index], content: next[index].content + delta }
                return next
              })
            }
            if (event.event === 'reasoning.summary') {
              const summary = publicReasoningSummary(event.event, event.payload)
              if (!summary) return
              streamed = true
              setMessages(old => {
                const index = old.findIndex(item => item.role === 'assistant' && item.task_id === taskId)
                if (index < 0) return [...old, { role: 'assistant', content: '', reasoning: summary, task_id: taskId, created_at: new Date().toISOString() }]
                const next = [...old]
                next[index] = { ...next[index], reasoning: mergeReasoningSummaries(next[index].reasoning, summary) }
                return next
              })
            }
            if (event.event === 'usage.updated') setUsage(event.payload as unknown as TokenUsage)
            const result = event.payload.result
            if (result && typeof result === 'object' && (result as ChatResult).task_id === taskId) finalResult = result as ChatResult
          }, controller.signal, cursor)
          if (!ownsStream()) return
          if (finalResult || resumeRejected) break
          const snapshot = await api<TaskSnapshot>(`/api/tasks/${taskId}`)
          finalResult = currentResult(snapshot)
          if (afterId > 0 && snapshot.event_cursor !== afterId) break
          if (!finalResult && !['pending', 'running'].includes(snapshot.status)) break
        } catch (caught) {
          if (!ownsStream() || reconnects >= 5) throw caught
          reconnects += 1
          await new Promise(resolve => setTimeout(resolve, Math.min(250 * (2 ** reconnects), 3000)))
        }
      }
      if (!finalResult && !resumeRejected) {
        const snapshot = await api<TaskSnapshot>(`/api/tasks/${taskId}`)
        finalResult = currentResult(snapshot)
      }
      if (!ownsStream()) return
      tts.flush(taskId, taskId)
      if (finalResult) await applyResult(finalResult, streamed)
      if (resumeRejected) await refreshRecoverable().catch(() => undefined)
      if (active && ownsStream()) {
        const [queueItems, activeTasks] = await Promise.all([
          refreshQueue(active.id).catch(() => []),
          api<TaskSnapshot[]>(`/api/tasks?conversation_id=${active.id}&active=true`),
        ])
        if (!ownsStream()) return
        const nextTask = attachableTask(activeTasks, queueItems, active.id, taskId)
          ?? await queueTaskReadback(queueItems, active.id, taskId)
        if (nextTask && ownsStream()) {
          const nextCursor = attachmentCursor(nextTask)
          const nextController = new AbortController()
          controllerRef.current = nextController
          runningTaskRef.current = nextTask.id
          setRunningTaskId(nextTask.id)
          setBusy(true)
          await attachTask(nextTask.id, nextController, nextCursor)
        }
      }
    } catch (caught) {
      if (ownsStream() && (caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (scope.accepts(owner) && runningTaskRef.current === taskId && controllerRef.current === controller) {
        runningTaskRef.current = null
        controllerRef.current = null
        setRunningTaskId(null)
        setBusy(false)
      }
    }
  }

  function markVoiceSessionBound(voiceSessionId: string | null) {
    if (!scope.accepts(owner)) return
    if (!voiceSessionId || voiceSessionRef.current?.id !== voiceSessionId) return
    voiceSessionRef.current = null
    setLastBoundVoiceSessionId(voiceSessionId)
  }

  async function submitFreshTask(taskId: string, conversationId: number, options: RequestInit) {
    const stillCurrent = () => scope.accepts(owner) && !options.signal?.aborted
    try {
      return await api<TaskSnapshot>('/api/tasks', options)
    } catch (caught) {
      // A lost POST response is not evidence that the durable intake failed.
      // Only look up this intent's ID. Never replay the POST or a resume.
      if (!stillCurrent() || (caught as Error).name === 'AbortError' ||
        (caught instanceof ApiError && caught.status < 500 && ![408, 409].includes(caught.status))) throw caught
      const timeout = AbortSignal.timeout(4000)
      const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout
      const receipt = await api<TaskSnapshot>(`/api/tasks/${encodeURIComponent(taskId)}`, {
        signal, cache: 'no-store',
      }).catch(() => null)
      if (!stillCurrent()) throw caught
      if (receipt?.id === taskId && receipt.conversation_id === conversationId) return receipt
      throw new Error('未确认任务是否已入队。司忆没有自动重发；请先刷新会话核对，再决定是否重试。')
    }
  }

  function restoreFreshDraft(taskId: string, content: string, caught: unknown) {
    if (!scope.accepts(owner) || (caught as Error).name === 'AbortError') return
    setMessages(old => old.filter(item => item.role !== 'user' || item.task_id !== taskId))
    setInput(current => current || content)
    setError((caught as Error).message)
  }

  async function send(content = input, approvedActions: string[] = [], existingTaskId?: string, approvalScope: 'once'|'task'|'session' = 'once') {
    if (!scope.accepts(owner)) return
    if (!content.trim()) return
    // Snapshot at entry. A Voice Session owns this exact submission even when
    // the transcript resembles a local slash command such as /stop or /clear.
    const voiceSessionId = existingTaskId ? null : voiceSessionRef.current?.id || null
    if (composerRoute(content, { existingTaskId, busy, voiceSessionId }) === 'command' && await runLocalCommand(content)) return
    if (!active) {
      setError('请先创建对话并选择工作区')
      return
    }
    if (busy && !existingTaskId) {
      const taskId = crypto.randomUUID()
      setError('')
      setMessages(old => [...old, { role: 'user', content, task_id: taskId, created_at: new Date().toISOString() }])
      setInput('')
      try {
        await submitFreshTask(taskId, active.id, {
          method: 'POST',
          body: JSON.stringify({
            conversation_id: active.id,
            content,
            task_id: taskId,
            approved_actions: sessionApprovalTokensRef.current,
            approval_scope: 'once',
            orchestration_mode: orchestrationMode,
            agent_count: orchestrationMode === 'single' ? 1 : 2,
            reasoning_effort: reasoningEffort,
            preferred_model: preferredModel || null,
            voice_session_id: voiceSessionId || undefined,
          }),
        })
      } catch (caught) {
        restoreFreshDraft(taskId, content, caught)
        return
      }
      if (!scope.accepts(owner)) return
      markVoiceSessionBound(voiceSessionId)
      // A failed queue read cannot undo accepted work.
      await refreshQueue(active.id).catch(() => undefined)
      return
    }
    if (busy) return
    await tts.interrupt(runningTaskRef.current)
    if (!scope.accepts(owner) || (existingTaskId && targetRef.current !== target)) return
    const taskId = existingTaskId || crypto.randomUUID()
    const controller = new AbortController()
    controllerRef.current = controller
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    setError('')
    setPendingReview(null)
    if (!approvedActions.length) {
      setMessages(old => [...old, { role: 'user', content, task_id: taskId, created_at: new Date().toISOString() }])
      setInput('')
    }
    try {
      const tokens = [...new Set([...sessionApprovalTokensRef.current, ...approvedActions])]
      const endpoint = existingTaskId ? `/api/tasks/${existingTaskId}/resume` : '/api/tasks'
      const body = existingTaskId ? {
        approved_actions: tokens,
        approval_scope: approvalScope,
      } : {
        conversation_id: active.id,
        content,
        task_id: taskId,
        approved_actions: tokens,
        approval_scope: approvalScope,
        orchestration_mode: orchestrationMode,
        agent_count: orchestrationMode === 'single' ? 1 : 2,
        reasoning_effort: reasoningEffort,
        preferred_model: preferredModel || null,
        voice_session_id: voiceSessionId || undefined,
      }
      const request: RequestInit = {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify(body),
      }
      const submitted = existingTaskId
        ? await api<TaskSnapshot>(endpoint, request)
        : await submitFreshTask(taskId, active.id, request)
      if (!scope.accepts(owner) || controller.signal.aborted) return
      markVoiceSessionBound(voiceSessionId)
      await refreshQueue(active.id).catch(() => undefined)
      if (!scope.accepts(owner)) return
      await attachTask(taskId, controller, existingTaskId ? resumeCursor(submitted) : 0)
    } catch (caught) {
      if (!existingTaskId) restoreFreshDraft(taskId, content, caught)
      else if (scope.accepts(owner) && (caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (scope.accepts(owner) && runningTaskRef.current === taskId && controllerRef.current === controller) {
        setBusy(false)
        setRunningTaskId(null)
        runningTaskRef.current = null
        controllerRef.current = null
      }
    }
  }

  async function steer(content = input) {
    if (composerRoute(content, { busy, steer: true }) === 'command' && await runLocalCommand(content)) return
    const taskId = runningTaskRef.current
    if (!active || !taskId || !content.trim()) return
    const requestId = crypto.randomUUID()
    setError('')
    try {
      const item = await api<ConversationQueueItem>(`/api/tasks/${taskId}/steer`, {
        method: 'POST',
        body: JSON.stringify({ content, priority: 'now', target_scope: 'task', request_id: requestId }),
      })
      setMessages(old => [...old, { role: 'user', content, task_id: `queue:${item.id}`, created_at: new Date().toISOString() }])
      setInput('')
      await refreshQueue(active.id)
    } catch (caught) {
      setError((caught as Error).message)
    }
  }

  async function promoteQueued(itemId: string) {
    await api(`/api/queue/${itemId}/promote`, { method: 'POST', body: JSON.stringify({ priority: 'next' }) })
    await refreshQueue()
  }

  async function cancelQueued(itemId: string) {
    const item = queued.find(candidate => candidate.id === itemId)
    await api(`/api/queue/${itemId}`, { method: 'DELETE' })
    if (item) {
      const localTaskId = item.kind === 'steer' ? `queue:${item.id}` : item.task_id
      setMessages(old => old.filter(message => message.task_id !== localTaskId))
    }
    await refreshQueue()
  }

  function setReasoningEffort(value: ReasoningEffort) {
    setReasoningEffortState(value)
    localStorage.setItem(REASONING_EFFORT_KEY, value)
  }

  function setPreferredModel(value: string) {
    setPreferredModelState(value)
    if (value) localStorage.setItem(PREFERRED_MODEL_KEY, value)
    else localStorage.removeItem(PREFERRED_MODEL_KEY)
  }

  async function interruptTts() {
    await tts.interrupt(runningTaskRef.current)
  }

  function acceptVoiceTranscription(input: VoiceTranscriptInput) {
    if (!scope.accepts(owner)) return
    voiceSessionRef.current = { id: input.voiceSessionId }
    setLastBoundVoiceSessionId(null)
    setInput(input.text)
    if (input.autoSend) dispatchVoiceTranscript(input.text, send, caught => setError((caught as Error).message))
  }

  function discardVoiceTranscription(voiceSessionId: string) {
    if (!scope.accepts(owner)) return
    if (voiceSessionRef.current?.id !== voiceSessionId) return
    voiceSessionRef.current = null
    setInput('')
  }

  async function stopTaskWithPolicy(strict: boolean) {
    if (!scope.accepts(owner)) return
    const taskId = runningTaskRef.current
    if (!taskId) {
      // Global stop also owns a live microphone or reviewed voice draft even
      // before it has become an Agent task.
      window.dispatchEvent(new Event('siyi:voice-stop'))
      await api('/api/voice/stop', { method: 'POST', body: '{}' }).catch(() => undefined)
      if (voiceSessionRef.current) {
        voiceSessionRef.current = null
        setInput('')
      }
      if (strict) throw new Error('当前没有可停止的运行任务')
      return
    }
    try {
      setStopState('stopping')
      window.dispatchEvent(new Event('siyi:voice-stop'))
      const voiceStop = await api<unknown>('/api/voice/stop', { method: 'POST', body: '{}' }).catch(() => undefined)
      if (!scope.accepts(owner)) return
      if (voiceSessionRef.current) {
        voiceSessionRef.current = null
        setInput('')
      }
      const authority = resolveVoiceTaskStopAuthority(taskId, voiceStop)
      let finalStatus = authority.status
      let settled = authority.settled
      let voiceSettled = authority.coveredByVoice && isVoiceStopAggregateSettled(voiceStop)
      if (!authority.coveredByVoice) {
        const result = await api<{ status: string }>(`/api/tasks/${taskId}/cancel`, { method: 'POST' })
        if (!['cancel_requested', 'cancelled'].includes(result.status)) throw new Error(`任务当前状态为 ${result.status}，未接受停止请求`)
        finalStatus = result.status
        settled = result.status === 'cancelled'
      }
      if (!scope.accepts(owner)) return
      controllerRef.current?.abort()
      for (let attempt = 0; attempt < 30 && (!settled || (authority.coveredByVoice && !voiceSettled)); attempt += 1) {
        await new Promise(resolve => window.setTimeout(resolve, 100))
        finalStatus = (await api<TaskSnapshot>(`/api/tasks/${taskId}`)).status
        if (authority.coveredByVoice) {
          const aggregate = await api<unknown>('/api/voice/stop', { method: 'POST', body: '{}' })
          const refreshed = resolveVoiceTaskStopAuthority(taskId, aggregate)
          settled = refreshed.settled
          voiceSettled = isVoiceStopAggregateSettled(aggregate)
        } else {
          settled = isFinalTaskStatus(finalStatus)
        }
      }
      if (!settled || (authority.coveredByVoice && !voiceSettled) || (!authority.coveredByVoice && finalStatus !== 'cancelled')) {
        throw new Error(`停止未完成，任务状态为 ${finalStatus || 'unknown'}`)
      }
      if (!scope.accepts(owner)) return
      runningTaskRef.current = null
      controllerRef.current = null
      setBusy(false)
      setRunningTaskId(null)
      setPendingReview(old => old?.taskId === taskId ? null : old)
      setStopState('stopped')
      if (!strict) {
        const content = finalStatus === 'cancelled'
          ? '任务已取消。已完成的操作会保留在审计记录中。'
          : '语音已停止，任务已结束。'
        setMessages(old => [...old, { role: 'assistant', content, created_at: new Date().toISOString() }])
      }
      recoveryLoadRef.current.begin()
      setRecoverableTasks(old => old.filter(task => task.id !== taskId))
    } catch (caught) {
      setStopState('failed')
      setError(`停止请求未确认：${(caught as Error).message}`)
      if (strict) throw caught
    }
  }

  async function stopTask() {
    await stopTaskWithPolicy(false)
  }

  async function resumeTask(options: ResumeOptions = {}) {
    if (!recoverable || busy || !scope.accepts(owner) || targetRef.current !== target) return
    const taskId = recoverable.id
    const controller = new AbortController()
    controllerRef.current = controller
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    setError('')
    setPendingReview(null)
    try {
      const resumed = await api<TaskSnapshot>(`/api/tasks/${taskId}/resume`, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          approved_actions: sessionApprovalTokensRef.current,
          approval_scope: 'once',
          checkpoint_sequence: options.checkpointSequence || selectedCheckpoint,
          allow_workspace_drift: Boolean(options.allowWorkspaceDrift),
          retry_uncertain: Boolean(options.retryUncertain),
        }),
      })
      await attachTask(taskId, controller, resumeCursor(resumed))
    } catch (caught) {
      const apiError = caught as ApiError
      const detail = apiError.detail as { code?: string } | undefined
      if (detail?.code === 'workspace_drift') setDriftTaskId(taskId)
      if ((caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (scope.accepts(owner) && runningTaskRef.current === taskId && controllerRef.current === controller) {
        setBusy(false)
        setRunningTaskId(null)
        runningTaskRef.current = null
        controllerRef.current = null
      }
    }
  }

  async function abandonTask(taskId: string | null | undefined) {
    if (!taskId || !scope.accepts(owner) || targetRef.current !== target) return
    try {
      await api(`/api/tasks/${taskId}/abandon`, { method: 'POST' })
      recoveryLoadRef.current.begin()
      setRecoverableTasks(old => old.filter(task => task.id !== taskId))
      setPendingReview(old => old?.taskId === taskId ? null : old)
      setDriftTaskId(old => old === taskId ? null : old)
      setUncertainTaskId(old => old === taskId ? null : old)
      setMessages(old => [...old, { role: 'assistant', content: '已放弃继续执行，当前文件现场保持不变。', created_at: new Date().toISOString() }])
    } catch (caught) {
      setError((caught as Error).message)
    }
  }

  function abandonRecovery() { return abandonTask(recoverable?.id) }
  function rejectPending() { return abandonTask(pendingVisible ? pendingReview?.taskId : null) }

  function approve(action: PendingAction, approvalScope: 'once'|'task'|'session' = 'once') {
    if (!scope.accepts(owner) || targetRef.current !== target || busy || active?.permission_mode === 'readonly' || !pendingVisible
      || !pendingReview || !pending.some(item => item.approval_key === action.approval_key)) return
    if (approvalScope === 'session') sessionApprovalTokensRef.current = [...new Set([...sessionApprovalTokensRef.current, action.approval_key])]
    const lastUser = [...messages].reverse().find(item => item.role === 'user')?.content || '继续执行已确认操作'
    void send(lastUser, [action.approval_key], pendingReview.taskId, approvalScope)
  }

  async function compactContext() {
    if (!active || busy) return
    setBusy(true)
    setError('')
    try {
      setContext(await api<ContextStats>(`/api/conversations/${active.id}/compact`, { method: 'POST', body: JSON.stringify({ force: true }) }))
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return {
    messages, setMessages, input, setInput, busy, error, setError, pending, commands,
    context, verification, usage, queued, runtimeEvents, runningTaskId, stopState, recoverable, selectedCheckpoint, workspaceDrift, uncertainOperation, reasoningEffort, preferredModel, lastBoundVoiceSessionId, reviewStatus,
    endRef, loadConversation, resetConversation, send, steer, promoteQueued, cancelQueued, stopTask, resumeTask, abandonRecovery, rejectPending,
    setSelectedCheckpoint, setReasoningEffort, setPreferredModel, approve, compactContext, interruptTts, acceptVoiceTranscription, discardVoiceTranscription,
  }
}
