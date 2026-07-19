import { api } from './api'

export type AdminMemoryOperation = 'memory.create' | 'memory.update' | 'memory.delete' | 'memory_candidate.accept'

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
