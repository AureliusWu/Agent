export type PermissionMode = 'readonly' | 'ask' | 'agent' | 'full'
export type OrchestrationMode = 'auto' | 'single' | 'planner_executor' | 'generator_verifier' | 'parallel_explorers'
export type ReasoningEffort = 'auto' | 'low' | 'medium' | 'high'
export type View = 'chat' | 'projects' | 'search' | 'memory' | 'usage' | 'files' | 'extensions' | 'audit' | 'settings'

export interface Conversation {
  id: number
  title: string
  title_source?: 'manual' | 'auto' | 'fallback'
  title_locked?: boolean
  title_generated_at?: string | null
  title_version?: number
  workspace: string
  permission_mode: PermissionMode
  agent_profile_id: string
  updated_at: string
}

export interface AgentProfile {
  id: string
  name: string
  description: string
  tool_allowlist: string[]
  skill_tags: string[]
  completion_standards: string[]
  verifier_id: string
  default_permission: PermissionMode
  allow_mcp: boolean
  source: 'builtin' | 'extension'
  extension_id?: string | null
  extension_version?: string | null
}

export interface ExtensionPackage {
  extension_id: string
  version: string
  name: string
  description: string
  enabled: boolean
  rollback_available: boolean
  digest: string
  signature_status: 'verified' | 'unsigned'
  permissions: string[]
  contributions: { tools: number; skills: number; agents: number; ui: number }
  installed_at: string
  activated_at?: string | null
  last_error?: string | null
}

export interface Message {
  id?: number
  task_id?: string
  role: 'user' | 'assistant' | 'system'
  content: string
  reasoning?: string
  created_at?: string
  artifacts?: import('./shared/artifactDownloads').ArtifactDownload[]
}

export type QueuePriority = 'now' | 'next' | 'later'

export interface ConversationQueueItem {
  id: string
  conversation_id: number
  task_id?: string | null
  kind: 'submit' | 'resume' | 'steer' | 'system'
  content: string
  priority: QueuePriority
  status: 'pending' | 'claimed' | 'consumed' | 'cancelled'
  created_at: string
}

export interface TokenUsage {
  total_tokens: number
  input_tokens: number
  output_tokens: number
  cached_input_tokens: number
  uncached_input_tokens: number
  cache_write_tokens: number
  cache_hit_rate: number
  phase_usage: Record<string, {
    input_tokens: number
    cached_input_tokens: number
    uncached_input_tokens: number
    cache_write_tokens: number
    output_tokens: number
    total_tokens: number
  }>
  phase_tokens: Record<string, number>
  limit: number
  remaining_tokens: number
  percent: number
}

export interface RuntimeEvent {
  id: number
  event: string
  payload: Record<string, unknown>
  created_at: string
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
  provider?: string
  latency_ms: number | null
  model: string
  error?: string
  error_type?: string
  action?: string
  version?: string
  service_status?: string
  models?: Array<{ name: string; size: number; modified_at: string }>
  first_load_hint?: string
  failure_category?: string
  capabilities?: Record<string, boolean | number | string | null>
}

export interface ProviderConfiguration {
  provider_id: 'deepseek' | 'ollama' | 'openai_compatible' | 'mock'
  base_url: string
  model: string
  timeout_seconds: number
  max_tokens: number
  max_retries: number
  allow_tools: boolean
  allow_streaming: boolean
}

export interface ProviderDescriptor {
  provider_id: ProviderConfiguration['provider_id']
  display_name: string
  provider_type: 'cloud' | 'remote' | 'local' | 'test'
  endpoint: string
  model: string
  credential_policy: 'required' | 'optional' | 'forbidden'
  capabilities: Record<string, boolean | number | string | null>
  timeout: number
  retry_policy: { max_retries: number; backoff: string; retryable_errors: string[] }
  local: boolean
  health_strategy: string
}

export interface ProviderCapability {
  provider: string
  model: string
  status: string
  capabilities: Record<'streaming' | 'native_tool_calls' | 'vision' | 'audio' | 'reasoning_effort' | 'json_mode' | 'embeddings', 'supported' | 'unsupported' | 'unknown'>
  latency_ms: number | null
  sample_count: number
  success_count: number
  stale: boolean
}

export interface ProviderProfile {
  id: string
  name: string
  official_url: string
  docs_url: string
  api_format: string
  request_url: string
  chat_endpoint: string
  credential_env: string
  default_model: string
  models: string[]
  thinking_modes?: string[]
  reasoning_efforts?: string[]
  deprecated_models?: string[]
  local?: boolean
  capabilities?: Record<string, boolean | number | string | null>
}

export interface ProviderPolicy {
  enabled: boolean
  escalation_enabled: boolean
  data_routing_enabled: boolean
  models: Record<string, string>
  provider: ProviderProfile
  capability_matrix: ProviderCapability[]
  model_performance: Record<string, { samples: number; success_rate: number; average_latency_ms: number; average_cost_usd: number | null; cost_status: 'known' | 'unknown' | 'partial'; known_cost_usd: number; unknown_cost_requests: number; pending_cost_requests?: number }>
}

export interface VerificationCheck {
  criterion_id?: string
  requirement_id?: string
  verifier?: string
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
  requirement_evidence?: Array<{
    requirement_id: string
    description: string
    status: VerificationCheck['status']
    verifier: string
    evidence: unknown
  }>
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
  namespace: 'project' | 'personal'
  category: MemoryCategory
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
  record_id?: string
  owner_api: 'scoped' | 'long_term'
  editable_via_current_api: boolean
  read_only_compatibility: boolean
}

export type MemoryCategory = 'architecture' | 'build_command' | 'test_command' | 'coding_convention' | 'decision' | 'known_issue' | 'successful_fix' | 'failed_approach' | 'user_constraint'

export type LongTermMemoryType = 'semantic' | 'episodic' | 'procedural' | 'relationship'
export interface LongTermMemory {
  id: string
  memory_type: LongTermMemoryType
  title: string | null
  content: string
  source_type: string
  confidence: number
  importance: number
  emotional_weight: number
  status: string
  user_confirmed: boolean
  is_locked: boolean
  is_sensitive: boolean
  occurred_at: string | null
  valid_from: string | null
  valid_until: string | null
  created_at: string
  updated_at: string
  metadata: Record<string, unknown>
  owner_api: 'scoped' | 'long_term'
  editable_via_current_api: boolean
  read_only_compatibility: boolean
}

export interface GlobalMemorySearchItem extends WorkspaceMemory {
  search_scope: 'global' | 'project'
  matched_terms: string[]
  score: number
}

export interface GlobalMemorySearchResponse {
  query: string
  items: GlobalMemorySearchItem[]
  counts: { global: number; project: number }
}

export interface CommandDefinition {
  name: string
  title: string
  description: string
  usage: string
  category: 'conversation' | 'context' | 'runtime' | 'memory' | 'diagnostics'
  execution: 'frontend' | 'backend' | 'runtime-control'
  risk: 'none' | 'confirm'
  requires_argument: boolean
  requires_conversation: boolean
  requires_workspace: boolean
  allowed_while_busy: boolean
  accepts_arguments: boolean
}

export interface CommandValidation {
  status: 'ready' | 'confirmation_required' | 'disabled' | 'error'
  code: string
  message: string
  command: string
  argument: string
}

export interface LongTermMemorySearchItem extends LongTermMemory {
  namespace: 'personal_long_term'
  snippet: string
  tags: string[]
  matched_fields: string[]
  matched_terms: string[]
  score: number
  ranking: { lexical: number; fts_rank: number | null; importance: number; confidence: number; recency: number; retrieval: number }
}

export interface LongTermMemorySearchResponse {
  query: string
  items: LongTermMemorySearchItem[]
  page: { offset: number; limit: number; total: number; has_more: boolean; next_cursor: string | null }
  filters: { memory_types: LongTermMemoryType[]; statuses: string[]; source_types: string[]; user_confirmed: boolean | null; is_locked: boolean | null; valid_from: string | null; valid_to: string | null; min_importance: number | null; min_confidence: number | null; sensitive_mode: 'exclude' | 'redacted' | 'full'; sort: 'relevance' | 'updated' | 'importance' }
}

export interface RecoverableTask {
  id: string
  conversation_id: number
  status: 'waiting_confirmation' | 'waiting_provider' | 'waiting_provider_credential' | 'interrupted' | 'timed_out'
  prompt: string
  current_phase: string
  current_step?: string
  termination_reason?: string
  checkpoint_sequence: number
  checkpoints: TaskCheckpoint[]
}
