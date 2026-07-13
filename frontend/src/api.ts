import { invoke } from '@tauri-apps/api/core'
import { getDesktopSecret, isDesktop } from './secrets'

interface BackendHealth { port: number | null; ready: boolean; error: string | null }

const WEB_API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
let cachedApiBase: string | null = null

export async function getApiBase(): Promise<string> {
  if (cachedApiBase) return cachedApiBase
  if (!isDesktop()) return WEB_API_BASE
  const status = await invoke<BackendHealth>('backend_status')
  if (!status.ready || !status.port) throw new Error(status.error || '本地后端尚未就绪')
  cachedApiBase = `http://127.0.0.1:${status.port}`
  return cachedApiBase
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const needsModelKey = path === '/api/chat' || path === '/api/provider/health' || path.endsWith('/compact')
  const [apiBase, desktopKey] = await Promise.all([
    getApiBase(),
    needsModelKey ? getDesktopSecret('model_api_key').catch(() => null) : Promise.resolve(null),
  ])
  let response: Response
  try {
    response = await fetch(`${apiBase}${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(desktopKey ? { 'X-Model-Api-Key': desktopKey } : {}), ...(options?.headers || {}) },
    })
  } catch (error) {
    if (isDesktop()) cachedApiBase = null
    throw new Error(`无法连接本地后端：${(error as Error).message}`)
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail || `HTTP ${response.status}`)
  }
  return response.json()
}

export const WEB_API_ADDRESS = WEB_API_BASE.replace(/^https?:\/\//, '')
