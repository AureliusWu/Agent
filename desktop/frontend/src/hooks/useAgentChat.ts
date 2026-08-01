import { useEffect, useRef, useState } from 'react'
import { api, ApiError, streamTaskEvents } from '../api'
import { executeLocalCommand, loadCommandCatalog } from '../commands/commandRegistry'
import { composerRoute } from '../commands/commandRoute'
import { mergeReasoningSummaries, publicReasoningSummary } from '../reasoningEvents'
import { extractArtifactDownloads, mergeArtifactDownloads } from '../shared/artifactDownloads'
import type { CommandDefinition, ContextStats, Conversation, ConversationQueueItem, Message, PendingAction, ReasoningEffort, RecoverableTask, RuntimeEvent, TokenUsage, VerificationReport, View } from '../types'

const REASONING_EFFORT_KEY = 'agent_reasoning_effort'
const PREFERRED_MODEL_KEY = 'agent_preferred_model'
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
  status: string
  result?: ChatResult
}

export type StopState = 'idle' | 'stopping' | 'stopped' | 'failed'

export function useAgentChat(active: Conversation | null, refreshConversations: () => void, navigate: (view: View, query?: string) => void) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState<PendingAction[]>([])
  const [context, setContext] = useState<ContextStats | null>(null)
  const [runningTaskId, setRunningTaskId] = useState<string | null>(null)
  const [stopState, setStopState] = useState<StopState>('idle')
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(null)
  const [verification, setVerification] = useState<VerificationReport | null>(null)
  const [recoverable, setRecoverable] = useState<RecoverableTask | null>(null)
  const [selectedCheckpoint, setSelectedCheckpoint] = useState<number | null>(null)
  const [workspaceDrift, setWorkspaceDrift] = useState(false)
  const [uncertainOperation, setUncertainOperation] = useState(false)
  const [reasoningEffort, setReasoningEffortState] = useState<ReasoningEffort>(savedReasoningEffort)
  const [preferredModel, setPreferredModelState] = useState(() => localStorage.getItem(PREFERRED_MODEL_KEY) || '')
  const [usage, setUsage] = useState<TokenUsage | null>(null)
  const [queued, setQueued] = useState<ConversationQueueItem[]>([])
  const [runtimeEvents, setRuntimeEvents] = useState<RuntimeEvent[]>([])
  const [commands, setCommands] = useState<CommandDefinition[]>([])
  const controllerRef = useRef<AbortController | null>(null)
  const runningTaskRef = useRef<string | null>(null)
  const sessionApprovalTokensRef = useRef<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, pending, recoverable])
  useEffect(() => () => controllerRef.current?.abort(), [])
  useEffect(() => { void loadCommandCatalog().then(setCommands).catch(caught => setError((caught as Error).message)) }, [])

  async function runLocalCommand(content: string): Promise<boolean> {
    if (!content.trim().startsWith('/')) return false
    setInput('')
    setError('')
    try {
      const catalog = commands.length ? commands : await loadCommandCatalog()
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
        navigate,
      })
    } catch (caught) {
      setError((caught as Error).message)
      return true
    }
  }

  async function refreshRecoverable(conversationId = active?.id) {
    if (!conversationId) {
      setRecoverable(null)
      return null
    }
    const tasks = await api<RecoverableTask[]>(`/api/tasks/recoverable?conversation_id=${conversationId}`)
    const latest = tasks[0] || null
    setRecoverable(latest)
    setSelectedCheckpoint(latest?.checkpoints[0]?.sequence || null)
    return latest
  }

  async function refreshQueue(conversationId = active?.id) {
    if (!conversationId) {
      setQueued([])
      return []
    }
    const items = await api<ConversationQueueItem[]>(`/api/conversations/${conversationId}/queue`)
    setQueued(items)
    return items
  }

  async function loadConversation(item: Conversation) {
    controllerRef.current?.abort()
    sessionApprovalTokensRef.current = []
    const [loadedMessages, stats, tasks, activeTasks, queueItems] = await Promise.all([
      api<Message[]>(`/api/conversations/${item.id}/messages`),
      api<ContextStats>(`/api/conversations/${item.id}/context`),
      api<RecoverableTask[]>(`/api/tasks/recoverable?conversation_id=${item.id}`),
      api<TaskSnapshot[]>(`/api/tasks?conversation_id=${item.id}&active=true`),
      api<ConversationQueueItem[]>(`/api/conversations/${item.id}/queue`),
    ])
    const latest = tasks[0] || null
    setMessages(loadedMessages)
    setContext(stats)
    setPending([])
    setPendingTaskId(null)
    setVerification(null)
    setUsage(null)
    setQueued(queueItems)
    setRecoverable(latest)
    setSelectedCheckpoint(latest?.checkpoints[0]?.sequence || null)
    setWorkspaceDrift(false)
    setUncertainOperation(false)
    if (activeTasks[0]) {
      const controller = new AbortController()
      controllerRef.current = controller
      void attachTask(activeTasks[0].id, controller)
    }
  }

  function resetConversation() {
    controllerRef.current?.abort()
    setMessages([])
    setPending([])
    setPendingTaskId(null)
    setVerification(null)
    setRecoverable(null)
    setSelectedCheckpoint(null)
    setWorkspaceDrift(false)
    setUncertainOperation(false)
    setContext(null)
    setInput('')
    setUsage(null)
    setQueued([])
    setRuntimeEvents([])
    setBusy(false)
    setRunningTaskId(null)
    runningTaskRef.current = null
    sessionApprovalTokensRef.current = []
  }

  async function applyResult(result: ChatResult, streamed = false) {
    const resultArtifacts = extractArtifactDownloads(result)
    if (result.task_status !== 'cancelled') {
      setMessages(old => {
        const index = old.findIndex(item => item.task_id === result.task_id)
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
    setPending(result.pending_actions || [])
    setPendingTaskId(result.pending_actions?.length ? result.task_id : null)
    setVerification(result.verification || null)
    if (result.usage) setUsage(result.usage)
    setUncertainOperation(Boolean(result.recovery?.retry_requires_confirmation))
    setWorkspaceDrift(false)
    if (result.context) setContext(result.context)
    await refreshRecoverable()
    refreshConversations()
    window.setTimeout(refreshConversations, 750)
    window.setTimeout(refreshConversations, 2500)
  }

  async function attachTask(taskId: string, controller: AbortController) {
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    let streamed = false
    let finalResult: ChatResult | undefined
    let cursor = 0
    let reconnects = 0
    try {
      while (!finalResult) {
        try {
          await streamTaskEvents(taskId, event => {
            cursor = Math.max(cursor, event.id)
            const artifactDownloads = extractArtifactDownloads(event.payload, String(event.payload.tool || ''))
            if (artifactDownloads.length > 0) {
              streamed = true
              setMessages(old => {
                const index = old.findIndex(item => item.task_id === taskId)
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
              setMessages(old => {
                const index = old.findIndex(item => item.task_id === taskId)
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
                const index = old.findIndex(item => item.task_id === taskId)
                if (index < 0) return [...old, { role: 'assistant', content: '', reasoning: summary, task_id: taskId, created_at: new Date().toISOString() }]
                const next = [...old]
                next[index] = { ...next[index], reasoning: mergeReasoningSummaries(next[index].reasoning, summary) }
                return next
              })
            }
            if (event.event === 'usage.updated') setUsage(event.payload as unknown as TokenUsage)
            const result = event.payload.result
            if (result && typeof result === 'object') finalResult = result as ChatResult
          }, controller.signal, cursor)
          if (finalResult) break
          const snapshot = await api<TaskSnapshot>(`/api/tasks/${taskId}`)
          finalResult = snapshot.result
          if (!finalResult && !['pending', 'running'].includes(snapshot.status)) break
        } catch (caught) {
          if (controller.signal.aborted || reconnects >= 5) throw caught
          reconnects += 1
          await new Promise(resolve => setTimeout(resolve, Math.min(250 * (2 ** reconnects), 3000)))
        }
      }
      if (!finalResult) {
        const snapshot = await api<TaskSnapshot>(`/api/tasks/${taskId}`)
        finalResult = snapshot.result
      }
      if (finalResult) await applyResult(finalResult, streamed)
      if (active) {
        const queueItems = await refreshQueue(active.id)
        const nextTaskId = queueItems.find(item => (item.kind === 'submit' || item.kind === 'resume') && item.task_id !== taskId)?.task_id
        if (nextTaskId) {
          const nextController = new AbortController()
          controllerRef.current = nextController
          runningTaskRef.current = nextTaskId
          setRunningTaskId(nextTaskId)
          setBusy(true)
          await attachTask(nextTaskId, nextController)
        }
      }
    } catch (caught) {
      if ((caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (runningTaskRef.current === taskId) {
        runningTaskRef.current = null
        controllerRef.current = null
        setRunningTaskId(null)
        setBusy(false)
      }
    }
  }

  async function send(content = input, approvedActions: string[] = [], existingTaskId?: string, approvalScope: 'once'|'task'|'session' = 'once') {
    if (!content.trim()) return
    if (composerRoute(content, { existingTaskId, busy }) === 'command' && await runLocalCommand(content)) return
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
        await api<TaskSnapshot>('/api/tasks', {
          method: 'POST',
          body: JSON.stringify({
            conversation_id: active.id,
            content,
            task_id: taskId,
            approved_actions: sessionApprovalTokensRef.current,
            approval_scope: 'once',
            orchestration_mode: 'single',
            agent_count: 1,
            reasoning_effort: reasoningEffort,
            preferred_model: preferredModel || null,
          }),
        })
        await refreshQueue(active.id)
      } catch (caught) {
        setMessages(old => old.filter(item => item.task_id !== taskId))
        setInput(content)
        setError((caught as Error).message)
      }
      return
    }
    if (busy) return
    const taskId = existingTaskId || crypto.randomUUID()
    const controller = new AbortController()
    controllerRef.current = controller
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    setError('')
    setPending([])
    if (!approvedActions.length) {
      setMessages(old => [...old, { role: 'user', content, created_at: new Date().toISOString() }])
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
        orchestration_mode: 'single',
        agent_count: 1,
        reasoning_effort: reasoningEffort,
        preferred_model: preferredModel || null,
      }
      await api<TaskSnapshot>(endpoint, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify(body),
      })
      await refreshQueue(active.id)
      await attachTask(taskId, controller)
    } catch (caught) {
      if ((caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (runningTaskRef.current === taskId) {
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
    setError('')
    try {
      const item = await api<ConversationQueueItem>(`/api/tasks/${taskId}/steer`, {
        method: 'POST',
        body: JSON.stringify({ content, priority: 'now', target_scope: 'task' }),
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

  async function stopTaskWithPolicy(strict: boolean) {
    const taskId = runningTaskRef.current
    if (!taskId) {
      if (strict) throw new Error('当前没有可停止的运行任务')
      return
    }
    try {
      setStopState('stopping')
      const result = await api<{ status: string }>(`/api/tasks/${taskId}/cancel`, { method: 'POST' })
      if (!['cancel_requested', 'cancelled'].includes(result.status)) throw new Error(`任务当前状态为 ${result.status}，未接受停止请求`)
      controllerRef.current?.abort()
      let finalStatus = result.status
      for (let attempt = 0; attempt < 30 && finalStatus === 'cancel_requested'; attempt += 1) {
        await new Promise(resolve => window.setTimeout(resolve, 100))
        finalStatus = (await api<TaskSnapshot>(`/api/tasks/${taskId}`)).status
      }
      if (finalStatus !== 'cancelled') throw new Error(`停止未完成，任务状态为 ${finalStatus}`)
      runningTaskRef.current = null
      controllerRef.current = null
      setBusy(false)
      setRunningTaskId(null)
      setPending([])
      setPendingTaskId(null)
      setStopState('stopped')
      if (!strict) setMessages(old => [...old, { role: 'assistant', content: '任务已取消。已完成的操作会保留在审计记录中。', created_at: new Date().toISOString() }])
      setRecoverable(null)
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
    if (!recoverable || busy) return
    const taskId = recoverable.id
    const controller = new AbortController()
    controllerRef.current = controller
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    setError('')
    setPending([])
    try {
      await api<TaskSnapshot>(`/api/tasks/${taskId}/resume`, {
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
      await attachTask(taskId, controller)
    } catch (caught) {
      const apiError = caught as ApiError
      const detail = apiError.detail as { code?: string } | undefined
      if (detail?.code === 'workspace_drift') setWorkspaceDrift(true)
      if ((caught as Error).name !== 'AbortError') setError((caught as Error).message)
    } finally {
      if (runningTaskRef.current === taskId) {
        setBusy(false)
        setRunningTaskId(null)
        runningTaskRef.current = null
        controllerRef.current = null
      }
    }
  }

  async function abandonRecovery() {
    const taskId = recoverable?.id || pendingTaskId
    if (!taskId) return
    try {
      await api(`/api/tasks/${taskId}/abandon`, { method: 'POST' })
      setRecoverable(null)
      setPending([])
      setPendingTaskId(null)
      setWorkspaceDrift(false)
      setUncertainOperation(false)
      setMessages(old => [...old, { role: 'assistant', content: '已放弃继续执行，当前文件现场保持不变。', created_at: new Date().toISOString() }])
    } catch (caught) {
      setError((caught as Error).message)
    }
  }

  function approve(action: PendingAction, scope: 'once'|'task'|'session' = 'once') {
    if (scope === 'session') sessionApprovalTokensRef.current = [...new Set([...sessionApprovalTokensRef.current, action.approval_key])]
    const lastUser = [...messages].reverse().find(item => item.role === 'user')?.content || '继续执行已确认操作'
    send(lastUser, [action.approval_key], pendingTaskId || undefined, scope)
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
    messages, setMessages, input, setInput, busy, error, setError, pending, setPending, commands,
    context, verification, usage, queued, runtimeEvents, runningTaskId, stopState, recoverable, selectedCheckpoint, workspaceDrift, uncertainOperation, reasoningEffort, preferredModel,
    endRef, loadConversation, resetConversation, send, steer, promoteQueued, cancelQueued, stopTask, resumeTask, abandonRecovery,
    setSelectedCheckpoint, setReasoningEffort, setPreferredModel, approve, compactContext,
  }
}
