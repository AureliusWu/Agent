import { invoke } from '@tauri-apps/api/core'
import { getDesktopSecret, isDesktop } from './secrets'

interface BackendHealth { port: number | null; ready: boolean; error: string | null }

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

const WEB_API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
let cachedApiBase: string | null = null
let cachedApiToken: string | null | undefined

export async function getApiBase(): Promise<string> {
  if (cachedApiBase) return cachedApiBase
  if (!isDesktop()) return WEB_API_BASE
  const status = await invoke<BackendHealth>('backend_status')
  if (!status.ready || !status.port) throw new Error(status.error || '本地后端尚未就绪')
  cachedApiBase = `http://127.0.0.1:${status.port}`
  return cachedApiBase
}

async function getApiToken(): Promise<string | null> {
  if (!isDesktop()) return null
  if (cachedApiToken !== undefined) return cachedApiToken
  cachedApiToken = await invoke<string>('backend_api_token')
  return cachedApiToken
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const needsModelKey = path === '/api/chat' || path === '/api/provider/health' || path.endsWith('/compact') || /\/api\/tasks\/[^/]+\/resume$/.test(path)
  const [apiBase, desktopKey, apiToken] = await Promise.all([
    getApiBase(),
    needsModelKey ? getDesktopSecret('model_api_key').catch(() => null) : Promise.resolve(null),
    getApiToken(),
  ])
  const headers = new Headers(options?.headers)
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (desktopKey) headers.set('X-Model-Api-Key', desktopKey)
  if (apiToken) headers.set('X-Agent-Api-Token', apiToken)
  try {
    return await fetch(`${apiBase}${path}`, {
      ...options,
      headers,
    })
  } catch (error) {
    if (isDesktop()) { cachedApiBase = null; cachedApiToken = undefined }
    throw new Error(`无法连接本地后端：${(error as Error).message}`)
  }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await apiFetch(path, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = body.detail
    const message = typeof detail === 'string' ? detail : (detail?.message || `HTTP ${response.status}`)
    throw new ApiError(message, response.status, detail)
  }
  return response.json()
}

export const WEB_API_ADDRESS = WEB_API_BASE.replace(/^https?:\/\//, '')
