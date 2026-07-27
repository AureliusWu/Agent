import { api } from '../api'
import type { CommandDefinition, CommandValidation, ContextStats, LongTermMemorySearchResponse, Message, TokenUsage, View } from '../types'

let catalogPromise: Promise<CommandDefinition[]> | null = null

export function loadCommandCatalog(): Promise<CommandDefinition[]> {
  catalogPromise ||= api<{ commands: CommandDefinition[] }>('/api/commands').then(result => result.commands)
  return catalogPromise.catch(error => {
    catalogPromise = null
    throw error
  })
}

interface CommandContext {
  text: string
  conversationId: number | null
  runningTaskId: string | null
  hasWorkspace: boolean
  waitingConfirmation: boolean
  recovering: boolean
  catalog: CommandDefinition[]
  append: (message: Message) => void
  clearMessages: () => void
  updateContext: (value: ContextStats) => void
  stopTask: () => Promise<void>
  navigate: (view: View, query?: string) => void
}

function systemMessage(command: string, content: string): Message {
  return {
    role: 'system',
    task_id: `local-command:${command}:${crypto.randomUUID()}`,
    content,
    created_at: new Date().toISOString(),
  }
}

async function validate(context: CommandContext, confirmed = false): Promise<CommandValidation> {
  return api<CommandValidation>('/api/commands/validate', {
    method: 'POST',
    body: JSON.stringify({
      text: context.text,
      has_conversation: Boolean(context.conversationId),
      has_workspace: context.hasWorkspace,
      running: Boolean(context.runningTaskId),
      waiting_confirmation: context.waitingConfirmation,
      recovering: context.recovering,
      confirmed,
    }),
  })
}

export async function executeLocalCommand(context: CommandContext): Promise<boolean> {
  if (!context.text.trim().startsWith('/')) return false
  let decision = await validate(context)
  if (decision.status === 'confirmation_required') {
    if (!window.confirm(`${decision.command}：${decision.message}，是否继续？`)) return true
    decision = await validate(context, true)
  }
  if (decision.status !== 'ready') throw new Error(decision.message)
  const conversationId = context.conversationId
  const handlers: Record<string, () => Promise<string | null>> = {
    '/help': async () => context.catalog.map(item => `${item.usage} — ${item.description}`).join('\n'),
    '/clear': async () => {
      await api(`/api/conversations/${conversationId}/messages`, { method: 'DELETE' })
      context.clearMessages()
      const stats = await api<ContextStats>(`/api/conversations/${conversationId}/context`)
      context.updateContext(stats)
      return '当前对话消息已清空。'
    },
    '/compact': async () => {
      const stats = await api<ContextStats>(`/api/conversations/${conversationId}/compact`, { method: 'POST', body: JSON.stringify({ force: true }) })
      context.updateContext(stats)
      return '上下文压缩已完成。'
    },
    '/context': async () => {
      const value = await api<ContextStats>(`/api/conversations/${conversationId}/context`)
      context.updateContext(value)
      return `当前上下文：\n\n\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\``
    },
    '/cost': async () => {
      const value = await api<TokenUsage & Record<string, unknown>>('/api/usage/summary')
      return `累计用量：\n\n\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\``
    },
    '/doctor': async () => {
      const value = await api<Record<string, unknown>>('/api/diagnostics/status')
      return `诊断状态：\n\n\`\`\`json\n${JSON.stringify(value, null, 2)}\n\`\`\``
    },
    '/memory': async () => {
      const value = await api<LongTermMemorySearchResponse>('/api/long-term-memories/search', {
        method: 'POST',
        body: JSON.stringify({ query: decision.argument, statuses: ['active'], sensitive_mode: 'exclude', limit: 10 }),
      })
      return value.items.length
        ? value.items.map(item => `- ${item.title || item.memory_type}：${item.content}`).join('\n')
        : `没有找到与“${decision.argument}”匹配的个人长期记忆。`
    },
    '/search': async () => { context.navigate('search', decision.argument); return null },
    '/stop': async () => { await context.stopTask(); return '任务取消已由 Runtime 控制面确认。' },
  }
  const handler = handlers[decision.command]
  if (!handler) throw new Error(`指令 ${decision.command} 没有受控执行器`)
  try {
    const result = await handler()
    await api('/api/commands/audit', { method: 'POST', body: JSON.stringify({ command: decision.command, status: 'ok', conversation_id: conversationId }) })
    if (result) context.append(systemMessage(decision.command, result))
  } catch (error) {
    await api('/api/commands/audit', { method: 'POST', body: JSON.stringify({ command: decision.command, status: 'error', conversation_id: conversationId }) }).catch(() => undefined)
    throw error
  }
  return true
}
