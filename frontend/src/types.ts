export type PermissionMode = 'ask' | 'agent' | 'full'
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
  risk?: 'low' | 'medium' | 'high' | 'critical'
  impact?: string
  source?: string
  allowed_scopes?: Array<'once' | 'task' | 'session'>
  expires_in_seconds?: number
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
  summary?: string
}

export interface ProviderHealth {
  status: 'ok' | 'error' | 'unconfigured'
  latency_ms: number | null
  model: string
  error?: string
}

export interface VerificationCheck {
  kind: 'file' | 'command' | 'response'
  target: unknown
  status: 'passed' | 'failed' | 'not_run' | 'unavailable'
  reason?: string
}

export interface VerificationReport {
  task_id: string
  status: 'passed' | 'failed' | 'partial'
  summary: string
  checks: VerificationCheck[]
  modified_files: string[]
  tool_run_count: number
  evaluation?: { score: number; tool_failures: number; basis: string }
}
