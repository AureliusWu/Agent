import type { OrchestrationMode, PermissionMode } from './types'

export const MODE_LABEL: Record<PermissionMode, string> = {
  ask: '请求批准',
  agent: '替我审批',
  full: '完全访问权限',
}

export const MODE_KEY = 'agent.permissionMode'
export const ACTIVE_CONVERSATION_KEY = 'agent.activeConversationId'
export const ORCHESTRATION_KEY = 'agent.orchestrationMode'

export const ORCHESTRATION_LABEL: Record<OrchestrationMode, string> = {
  single: '单 Agent',
  planner_executor: '规划执行',
  generator_verifier: '生成验证',
  parallel_explorers: '并行探索',
}

export function savedMode(): PermissionMode {
  const value = localStorage.getItem(MODE_KEY)
  if (value === 'ask' || value === 'agent' || value === 'full') return value
  if (value === 'auto') return 'full'
  return 'ask'
}

export function savedOrchestrationMode(): OrchestrationMode {
  const value = localStorage.getItem(ORCHESTRATION_KEY)
  if (value === 'planner_executor' || value === 'generator_verifier' || value === 'parallel_explorers') return value
  return 'single'
}
