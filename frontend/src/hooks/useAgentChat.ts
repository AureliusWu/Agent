import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { ContextStats, Conversation, Message, PendingAction, VerificationReport } from '../types'

interface ChatResult {
  content: string
  pending_actions: PendingAction[]
  context?: ContextStats
  task_status: string
  task_id: string
  verification?: VerificationReport
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
  const controllerRef = useRef<AbortController | null>(null)
  const runningTaskRef = useRef<string | null>(null)
  const sessionApprovalTokensRef = useRef<string[]>([])
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, pending])
  useEffect(() => () => controllerRef.current?.abort(), [])

  async function loadConversation(item: Conversation) {
    sessionApprovalTokensRef.current = []
    const [loadedMessages, stats] = await Promise.all([
      api<Message[]>(`/api/conversations/${item.id}/messages`),
      api<ContextStats>(`/api/conversations/${item.id}/context`),
    ])
    setMessages(loadedMessages)
    setContext(stats)
    setPending([])
    setPendingTaskId(null)
    setVerification(null)
  }

  function resetConversation() {
    controllerRef.current?.abort()
    setMessages([])
    setPending([])
    setPendingTaskId(null)
    setVerification(null)
    setContext(null)
    setInput('')
    setBusy(false)
    setRunningTaskId(null)
    runningTaskRef.current = null
    sessionApprovalTokensRef.current = []
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
        body: JSON.stringify({ conversation_id: active.id, content, task_id: taskId, approved_actions: tokens, approval_scope: approvalScope }),
      })
      if (result.task_status !== 'cancelled') {
        setMessages(old => [...old, { role: 'assistant', content: result.content }])
      }
      setPending(result.pending_actions || [])
      setPendingTaskId(result.pending_actions?.length ? result.task_id : null)
      setVerification(result.verification || null)
      if (result.context) setContext(result.context)
      refreshConversations()
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
    try {
      await cancelRequest
    } catch (caught) {
      setError(`停止请求未确认：${(caught as Error).message}`)
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
    context, verification, runningTaskId, endRef, loadConversation, resetConversation, send, stopTask,
    approve, compactContext,
  }
}
