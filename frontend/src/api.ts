import { getDesktopSecret } from './secrets'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const needsModelKey = path === '/api/chat' || path === '/api/provider/health' || path.endsWith('/compact')
  const desktopKey = needsModelKey ? await getDesktopSecret('model_api_key').catch(() => null) : null
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(desktopKey ? { 'X-Model-Api-Key': desktopKey } : {}), ...(options?.headers || {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

export { API_BASE }
