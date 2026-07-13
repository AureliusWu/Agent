import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { ORCHESTRATION_KEY, savedOrchestrationMode } from '../constants'
import type { ContextStats, Conversation, Message, OrchestrationMode, PendingAction, RecoverableTask, VerificationReport } from '../types'

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
    sessionApprovalTokensRef.current = []
    const [loadedMessages, stats, tasks] = await Promise.all([
      api<Message[]>(`/api/conversations/${item.id}/messages`),
      api<ContextStats>(`/api/conversations/${item.id}/context`),
      api<RecoverableTask[]>(`/api/tasks/recoverable?conversation_id=${item.id}`),
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

  async function applyResult(result: ChatResult) {
    if (result.task_status !== 'cancelled') setMessages(old => [...old, { role: 'assistant', content: result.content }])
    setPending(result.pending_actions || [])
    setPendingTaskId(result.pending_actions?.length ? result.task_id : null)
    setVerification(result.verification || null)
    setUncertainOperation(Boolean(result.recovery?.retry_requires_confirmation))
    setWorkspaceDrift(false)
    if (result.context) setContext(result.context)
    await refreshRecoverable()
    refreshConversations()
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
      const result = await api<ChatResult>('/api/chat', {
        method: 'POST',
        signal: controller.signal,
        body: JSON.stringify({
          conversation_id: active.id,
          content,
          task_id: taskId,
          approved_actions: tokens,
          approval_scope: approvalScope,
          orchestration_mode: orchestrationMode,
          agent_count: orchestrationMode === 'parallel_explorers' ? 3 : 1,
        }),
      })
      await applyResult(result)
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
      const result = await api<ChatResult>(`/api/tasks/${taskId}/resume`, {
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
      await applyResult(result)
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
    context, verification, runningTaskId, recoverable, selectedCheckpoint, workspaceDrift, uncertainOperation, orchestrationMode,
    endRef, loadConversation, resetConversation, send, pauseTask, stopTask, resumeTask, abandonRecovery,
    setSelectedCheckpoint, setOrchestrationMode, approve, compactContext,
  }
}
