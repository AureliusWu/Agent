import { useEffect, useRef, useState } from 'react'
import { api, ApiError, streamTaskEvents } from '../api'
import { ORCHESTRATION_KEY, savedOrchestrationMode } from '../constants'
import type { ContextStats, Conversation, Message, OrchestrationMode, PendingAction, ReasoningEffort, RecoverableTask, VerificationReport } from '../types'

const REASONING_EFFORT_KEY = 'agent_reasoning_effort'
const savedReasoningEffort = (): ReasoningEffort => {
  const value = localStorage.getItem(REASONING_EFFORT_KEY)
  return value === 'low' || value === 'medium' || value === 'high' ? value : 'auto'
}

interface ChatResult {
  content: string
  pending_actions: PendingAction[]
  context?: ContextStats
  task_status: string
  task_id: string
  resumable?: boolean
  verification?: VerificationReport
  recovery?: { execution_id: string; tool: string; retry_requires_confirmation: boolean }
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

export function useAgentChat(active: Conversation | null, refreshConversations: () => void) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState<PendingAction[]>([])
  const [context, setContext] = useState<ContextStats | null>(null)
  const [runningTaskId, setRunningTaskId] = useState<string | null>(null)
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(null)
  const [verification, setVerification] = useState<VerificationReport | null>(null)
  const [recoverable, setRecoverable] = useState<RecoverableTask | null>(null)
  const [selectedCheckpoint, setSelectedCheckpoint] = useState<number | null>(null)
  const [workspaceDrift, setWorkspaceDrift] = useState(false)
  const [uncertainOperation, setUncertainOperation] = useState(false)
  const [orchestrationMode, setOrchestrationModeState] = useState<OrchestrationMode>(savedOrchestrationMode)
  const [reasoningEffort, setReasoningEffortState] = useState<ReasoningEffort>(savedReasoningEffort)
  const controllerRef = useRef<AbortController | null>(null)
  const runningTaskRef = useRef<string | null>(null)
  const sessionApprovalTokensRef = useRef<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, pending, recoverable])
  useEffect(() => () => controllerRef.current?.abort(), [])

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

  async function loadConversation(item: Conversation) {
    controllerRef.current?.abort()
    sessionApprovalTokensRef.current = []
    const [loadedMessages, stats, tasks, activeTasks] = await Promise.all([
      api<Message[]>(`/api/conversations/${item.id}/messages`),
      api<ContextStats>(`/api/conversations/${item.id}/context`),
      api<RecoverableTask[]>(`/api/tasks/recoverable?conversation_id=${item.id}`),
      api<TaskSnapshot[]>(`/api/tasks?conversation_id=${item.id}&active=true`),
    ])
    const latest = tasks[0] || null
    setMessages(loadedMessages)
    setContext(stats)
    setPending([])
    setPendingTaskId(null)
    setVerification(null)
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
    setBusy(false)
    setRunningTaskId(null)
    runningTaskRef.current = null
    sessionApprovalTokensRef.current = []
  }

  async function applyResult(result: ChatResult, streamed = false) {
    if (result.task_status !== 'cancelled') {
      setMessages(old => {
        const index = old.findIndex(item => item.task_id === result.task_id)
        if (streamed && index >= 0) {
          const next = [...old]
          next[index] = { role: 'assistant', content: result.content }
          return next
        }
        return [...old, { role: 'assistant', content: result.content }]
      })
    }
    setPending(result.pending_actions || [])
    setPendingTaskId(result.pending_actions?.length ? result.task_id : null)
    setVerification(result.verification || null)
    setUncertainOperation(Boolean(result.recovery?.retry_requires_confirmation))
    setWorkspaceDrift(false)
    if (result.context) setContext(result.context)
    await refreshRecoverable()
    refreshConversations()
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
            if (event.event === 'model.delta') {
              const delta = String(event.payload.delta || '')
              if (!delta) return
              streamed = true
              setMessages(old => {
                const index = old.findIndex(item => item.task_id === taskId)
                if (index < 0) return [...old, { role: 'assistant', content: delta, task_id: taskId }]
                const next = [...old]
                next[index] = { ...next[index], content: next[index].content + delta }
                return next
              })
            }
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
    if (!content.trim() || busy) return
    if (!active) {
      setError('请先创建对话并选择工作区')
      return
    }
    const taskId = existingTaskId || crypto.randomUUID()
    const controller = new AbortController()
    controllerRef.current = controller
    runningTaskRef.current = taskId
    setRunningTaskId(taskId)
    setBusy(true)
    setError('')
    setPending([])
    if (!approvedActions.length) {
      setMessages(old => [...old, { role: 'user', content }])
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
        agent_count: orchestrationMode === 'parallel_explorers' ? 3 : 1,
        reasoning_effort: reasoningEffort,
      }
      await api<TaskSnapshot>(endpoint, {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify(body),
      })
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

  function setOrchestrationMode(value: OrchestrationMode) {
    setOrchestrationModeState(value)
    localStorage.setItem(ORCHESTRATION_KEY, value)
  }

  function setReasoningEffort(value: ReasoningEffort) {
    setReasoningEffortState(value)
    localStorage.setItem(REASONING_EFFORT_KEY, value)
  }

  async function pauseTask() {
    const taskId = runningTaskRef.current
    if (!taskId) return
    try {
      await api(`/api/tasks/${taskId}/pause`, { method: 'POST' })
      controllerRef.current?.abort()
      setMessages(old => [...old, { role: 'assistant', content: '任务已暂停，现场和检查点已保留。' }])
      await refreshRecoverable()
    } catch (caught) {
      setError(`暂停请求未确认：${(caught as Error).message}`)
    } finally {
      runningTaskRef.current = null
      controllerRef.current = null
      setBusy(false)
      setRunningTaskId(null)
      setPending([])
      setPendingTaskId(null)
    }
  }

  async function stopTask() {
    const taskId = runningTaskRef.current
    if (!taskId) return
    const cancelRequest = api(`/api/tasks/${taskId}/cancel`, { method: 'POST' })
    controllerRef.current?.abort()
    runningTaskRef.current = null
    controllerRef.current = null
    setBusy(false)
    setRunningTaskId(null)
    setPending([])
    setPendingTaskId(null)
    setMessages(old => [...old, { role: 'assistant', content: '任务已取消。已完成的操作会保留在审计记录中。' }])
    setRecoverable(null)
    try {
      await cancelRequest
    } catch (caught) {
      setError(`停止请求未确认：${(caught as Error).message}`)
    }
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
      setMessages(old => [...old, { role: 'assistant', content: '已放弃继续执行，当前文件现场保持不变。' }])
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
    messages, setMessages, input, setInput, busy, error, setError, pending, setPending,
    context, verification, runningTaskId, recoverable, selectedCheckpoint, workspaceDrift, uncertainOperation, orchestrationMode, reasoningEffort,
    endRef, loadConversation, resetConversation, send, pauseTask, stopTask, resumeTask, abandonRecovery,
    setSelectedCheckpoint, setOrchestrationMode, setReasoningEffort, approve, compactContext,
  }
}
