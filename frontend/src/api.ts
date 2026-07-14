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

export function getConfiguredApiAddress(): string {
  return WEB_API_BASE.replace(/^https?:\/\//, '')
}

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

function getWebApiKey(): string | null {
  if (isDesktop()) return null
  try {
    return localStorage.getItem('agent.webApiKey') || null
  } catch {
    return null
  }
}

export function saveWebApiKey(key: string): void {
  try {
    localStorage.setItem('agent.webApiKey', key)
  } catch { /* 无痕浏览可能不可用 */ }
}

export function hasWebApiKey(): boolean {
  return Boolean(getWebApiKey())
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
  const webKey = getWebApiKey()
  if (desktopKey) headers.set('X-Model-Api-Key', desktopKey)
  else if (webKey) headers.set('X-Model-Api-Key', webKey)
  if (apiToken) headers.set('X-Agent-Api-Token', apiToken)
  try {
    return await fetch(`${apiBase}${path}`, {
      ...options,
      headers,
    })
  } catch (error) {
    if (isDesktop()) { cachedApiBase = null; cachedApiToken = undefined }
    const msg = (error as Error).message || String(error)
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError') || msg.includes('fetch')) {
      if (!isDesktop()) {
        throw new Error(`后端未启动，请在 backend 目录执行 python run_server.py 启动后端服务（${apiBase}）`)
      }
      throw new Error(`本地后端连接断开，请重启应用（${apiBase}）`)
    }
    throw new Error(`无法连接本地后端：${msg}`)
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
