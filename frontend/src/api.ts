import { invoke } from '@tauri-apps/api/core'
import { waitForDesktopBackend } from './desktopRuntime'
import { getDesktopSecret, isDesktop } from './secrets'

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
let pendingDesktopApiBase: Promise<string> | null = null
let cachedDesktopApiToken: string | null | undefined
let webAccessToken: string | null = null

export function getConfiguredApiAddress(): string {
  return WEB_API_BASE.replace(/^https?:\/\//, '')
}

export async function getApiBase(): Promise<string> {
  if (cachedApiBase) return cachedApiBase
  if (!isDesktop()) return WEB_API_BASE
  if (!pendingDesktopApiBase) {
    pendingDesktopApiBase = waitForDesktopBackend()
      .then(status => {
        cachedApiBase = `http://127.0.0.1:${status.port}`
        return cachedApiBase
      })
      .finally(() => { pendingDesktopApiBase = null })
  }
  return pendingDesktopApiBase
}

export function clearDesktopApiCache(): void {
  cachedApiBase = null
  pendingDesktopApiBase = null
  cachedDesktopApiToken = undefined
}

async function getApiToken(): Promise<string | null> {
  if (!isDesktop()) return webAccessToken
  if (cachedDesktopApiToken !== undefined) return cachedDesktopApiToken
  cachedDesktopApiToken = await invoke<string>('backend_api_token')
  return cachedDesktopApiToken
}

export function setWebAccessToken(token: string): void {
  webAccessToken = token.trim() || null
}

export function hasWebAccessToken(): boolean {
  return Boolean(webAccessToken)
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const needsModelKey = path === '/api/chat' || path === '/api/tasks' || path === '/api/provider/health' || path.endsWith('/compact') || /\/api\/tasks\/[^/]+\/resume$/.test(path)
  const [apiBase, desktopModelKey, apiToken] = await Promise.all([
    getApiBase(),
    isDesktop() && needsModelKey ? getDesktopSecret('model_api_key').catch(() => null) : Promise.resolve(null),
    getApiToken(),
  ])
  const headers = new Headers(options?.headers)
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!isDesktop()) headers.delete('X-Model-Api-Key')
  if (desktopModelKey && !headers.has('X-Model-Api-Key')) headers.set('X-Model-Api-Key', desktopModelKey)
  if (apiToken) headers.set('X-Agent-Api-Token', apiToken)
  try {
    return await fetch(`${apiBase}${path}`, {
      ...options,
      headers,
    })
  } catch (error) {
    if (isDesktop()) {
      clearDesktopApiCache()
    }
    const msg = (error as Error).message || String(error)
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError') || msg.includes('fetch')) {
      if (!isDesktop()) {
        throw new Error(`无法连接后端服务（${apiBase}），请检查服务地址和网络状态`)
      }
      throw new Error(`本地核心连接中断（${apiBase}），请重试或使用状态栏重启核心`)
    }
    throw new Error(`无法连接后端：${msg}`)
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

export interface TaskStreamEvent {
  id: number
  task_id: string
  event: string
  payload: Record<string, unknown>
  created_at: string
}

export async function streamTaskEvents(
  taskId: string,
  onEvent: (event: TaskStreamEvent) => void | Promise<void>,
  signal: AbortSignal,
  afterId = 0,
): Promise<number> {
  const response = await apiFetch(`/api/tasks/${taskId}/events?after_id=${afterId}`, {
    signal,
    headers: { Accept: 'text/event-stream' },
  })
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new ApiError(String(body.detail || `HTTP ${response.status}`), response.status, body.detail)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let cursor = afterId
  while (true) {
    const { value, done } = await reader.read()
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
    let boundary = buffer.indexOf('\n\n')
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary)
      buffer = buffer.slice(boundary + 2)
      const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n')
      if (data) {
        const event = JSON.parse(data) as TaskStreamEvent
        cursor = Math.max(cursor, event.id)
        await onEvent(event)
      }
      boundary = buffer.indexOf('\n\n')
    }
    if (done) break
  }
  return cursor
}
