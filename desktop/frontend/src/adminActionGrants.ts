import { api } from './api'

export type AdminMemoryOperation = 'memory.create' | 'memory.update' | 'memory.delete' | 'memory.search_sensitive' | 'memory_candidate.accept'
export type AdminManagementOperation =
  | 'mcp.register'
  | 'mcp.enable'
  | 'mcp.disable'
  | 'mcp.delete'
  | 'mcp.test'
  | 'extension.install'
  | 'extension.enable'
  | 'extension.disable'
  | 'extension.uninstall'
  | 'extension.rollback'
  | 'skill.install'
  | 'skill.enable'
  | 'skill.disable'
  | 'skill.uninstall'

const SESSION_KEY = 'siyi.admin.ui-session'

export function adminUiSessionId(): string {
  const existing = sessionStorage.getItem(SESSION_KEY)
  if (existing) return existing
  const created = crypto.randomUUID()
  sessionStorage.setItem(SESSION_KEY, created)
  return created
}

export async function issueAdminActionGrant(
  operation: AdminMemoryOperation,
  targetId: string,
  payload: Record<string, unknown>,
  uiSessionId: string,
): Promise<string> {
  const result = await api<{ grant_token: string }>('/api/long-term-memories/admin-action-grants', {
    method: 'POST',
    body: JSON.stringify({ operation, target_id: targetId, payload, ui_session_id: uiSessionId }),
  })
  return result.grant_token
}

export async function managementActionHeaders(
  operation: AdminManagementOperation,
  targetId: string,
  payload: Record<string, unknown>,
  conversationId: number,
): Promise<Record<string, string>> {
  if (!Number.isInteger(conversationId) || conversationId <= 0) {
    throw new Error('请先选择一个对话，再执行管理员操作')
  }
  const uiSessionId = adminUiSessionId()
  const result = await api<{ grant_token: string }>('/api/admin-actions/grants', {
    method: 'POST',
    body: JSON.stringify({
      operation,
      target_id: targetId,
      payload,
      ui_session_id: uiSessionId,
      conversation_id: conversationId,
      administrator_confirmed: true,
    }),
  })
  return {
    'X-Siyi-Admin-Grant': result.grant_token,
    'X-Siyi-UI-Session': uiSessionId,
    'X-Siyi-Conversation-Id': String(conversationId),
  }
}
