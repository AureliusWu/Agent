import type { PermissionMode } from './types'

export const MODE_LABEL: Record<PermissionMode, string> = {
  ask: '请求批准',
  agent: '替我审批',
  full: '完全访问权限',
}

export const MODE_KEY = 'agent.permissionMode'
export const ACTIVE_CONVERSATION_KEY = 'agent.activeConversationId'

export function savedMode(): PermissionMode {
  const value = localStorage.getItem(MODE_KEY)
  if (value === 'ask' || value === 'agent' || value === 'full') return value
  if (value === 'auto') return 'full'
  return 'ask'
}
