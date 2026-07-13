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
  criterion_id?: string
  description?: string
  kind: string
  target?: unknown
  evidence?: unknown
  status: 'passed' | 'failed' | 'not_run' | 'unavailable'
  reason?: string
}

export interface VerificationReport {
  task_id: string
  status: 'passed' | 'failed' | 'partially_passed' | 'blocked'
  summary: string
  checks: VerificationCheck[]
  modified_files: string[]
  tool_run_count: number
  requirements_met?: VerificationCheck[]
  requirements_failed?: VerificationCheck[]
  retry_recommended?: boolean
  retry_scope?: string[]
  reason?: string
  side_effects?: string[]
  evaluation?: { score: number; tool_failures: number; basis: string }
}

export interface TaskCheckpoint {
  id: number
  task_id: string
  sequence: number
  phase: string
  reason: string
  workspace_hash: string
  git_status: string
  created_at: string
}

export interface WorkspaceMemory {
  id: number
  key: string
  content: string
  kind: 'project' | 'experience'
  source: string
  tags: string[]
  applicable_version: string | null
  confidence: number
  effective_confidence: number
  use_count: number
  success_count: number
  failure_count: number
  rejected: number
  status: 'active' | 'stale' | 'rejected'
  stale_reasons: string[]
  created_at: string
  updated_at: string
}

export interface RecoverableTask {
  id: string
  conversation_id: number
  status: 'waiting_confirmation' | 'paused' | 'interrupted' | 'timed_out'
  prompt: string
  current_phase: string
  current_step?: string
  termination_reason?: string
  checkpoint_sequence: number
  checkpoints: TaskCheckpoint[]
}
