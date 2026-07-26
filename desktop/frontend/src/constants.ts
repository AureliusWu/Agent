import type { OrchestrationMode, PermissionMode, ReasoningEffort } from './types'

export const MODE_LABEL: Record<PermissionMode, string> = {
  readonly: '只读',
  ask: '请求批准',
  agent: '替我审批',
  full: '完全访问权限',
}

export const MODE_DESCRIPTION: Record<PermissionMode, string> = {
  readonly: '允许读取，禁用所有写入和外部副作用工具',
  ask: '文件修改和危险操作会先询问',
  agent: '自动批准普通工作区操作',
  full: '允许当前工作区文件操作，关键命令仍会确认',
}

export const REASONING_LABEL: Record<ReasoningEffort, string> = {
  auto: '自动推理',
  low: '低推理',
  medium: '中推理',
  high: '高推理',
}

export const MODE_KEY = 'agent.permissionMode'
export const ACTIVE_CONVERSATION_KEY = 'agent.activeConversationId'
export const ORCHESTRATION_KEY = 'agent.orchestrationMode'
export const SIDEBAR_KEY = 'agent.sidebarExpanded'
export const WORKSPACE_KEY = 'agent.lastWorkspace'

export const ORCHESTRATION_LABEL: Record<OrchestrationMode, string> = {
  auto: '自动调度',
  single: '单 Agent',
  planner_executor: '规划执行',
  generator_verifier: '生成验证',
  parallel_explorers: '并行探索',
}

export function savedMode(): PermissionMode {
  const value = localStorage.getItem(MODE_KEY)
  if (value === 'readonly' || value === 'ask' || value === 'agent' || value === 'full') return value
  if (value === 'auto') return 'full'
  return 'ask'
}

export function savedOrchestrationMode(): OrchestrationMode {
  const value = localStorage.getItem(ORCHESTRATION_KEY)
  if (value === 'auto' || value === 'single' || value === 'planner_executor' || value === 'generator_verifier' || value === 'parallel_explorers') return value
  return 'auto'
}
