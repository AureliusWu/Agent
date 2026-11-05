export type AttentionStatus = 'waiting_confirmation' | 'waiting_user' | 'waiting_provider_credential' | 'waiting_provider' | 'interrupted' | 'timed_out'
type AttentionCategory = 'confirmation' | 'user_input' | 'credential' | 'provider' | 'recovery'

export interface TaskAttentionTarget {
  taskId: string
  conversationId: number
}

export interface TaskAttentionItem {
  task_id: string
  conversation_id: number
  status: AttentionStatus
  category: AttentionCategory
  updated_at: string
}

export interface TaskAttentionSnapshot {
  items: TaskAttentionItem[]
  total: number
}

const categories: Record<AttentionStatus, AttentionCategory> = {
  waiting_confirmation: 'confirmation',
  waiting_user: 'user_input',
  waiting_provider_credential: 'credential',
  waiting_provider: 'provider',
  interrupted: 'recovery',
  timed_out: 'recovery',
}

export const attentionStatusLabels: Record<AttentionStatus, string> = {
  waiting_confirmation: '等待确认',
  waiting_user: '等待补充信息',
  waiting_provider_credential: '等待模型授权',
  waiting_provider: '等待模型恢复',
  interrupted: '已中断',
  timed_out: '已超时',
}

// Keep an explicit projection on the UI side as well: additional server fields
// must never be cached or displayed, and a malformed count is not an empty inbox.
export function parseTaskAttention(value: unknown): TaskAttentionSnapshot {
  const invalid = () => new Error('人工待办响应格式无效')
  if (!value || typeof value !== 'object') throw invalid()
  const { items, total } = value as TaskAttentionSnapshot
  if (!Array.isArray(items) || items.length > 100 || !Number.isSafeInteger(total)
    || total < items.length || (total > 0 && !items.length)) throw invalid()
  const seen = new Set<string>()
  return {
    total,
    items: items.map(item => {
      if (!item || typeof item !== 'object') throw invalid()
      const { task_id, conversation_id, status, category, updated_at } = item
      if (typeof task_id !== 'string' || !/^[a-f0-9-]{16,64}$/.test(task_id) || seen.has(task_id)
        || !Number.isSafeInteger(conversation_id) || conversation_id < 1
        || !Object.hasOwn(categories, status) || categories[status] !== category
        || typeof updated_at !== 'string' || updated_at.length > 64 || !Number.isFinite(Date.parse(updated_at))) throw invalid()
      seen.add(task_id)
      return { task_id, conversation_id, status, category, updated_at }
    }),
  }
}
