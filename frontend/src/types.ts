export type PermissionMode = 'readonly' | 'confirm' | 'auto'
export type View = 'chat' | 'files' | 'extensions' | 'audit'

export interface Conversation {
  id: number
  title: string
  workspace: string
  permission_mode: PermissionMode
  updated_at: string
}

export interface Message {
  id?: number
  role: 'user' | 'assistant'
  content: string
}

export interface PendingAction {
  approval_key: string
  tool: string
  arguments: Record<string, unknown>
}

export interface FileItem {
  name: string
  path: string
  type: 'file' | 'directory'
  size?: number
}

export interface ContextStats {
  message_count: number
  estimated_tokens: number
  compacted_through: number
  has_summary: boolean
}

export interface ProviderHealth {
  status: 'ok' | 'error' | 'unconfigured'
  latency_ms: number | null
  model: string
  error?: string
}
