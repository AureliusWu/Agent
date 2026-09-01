import { invoke } from '@tauri-apps/api/core'
import { desktopEndpointEpoch, getDesktopBackendStatus, onDesktopEndpointChange, waitForDesktopBackend } from './desktopRuntime'
import { needsModelCredential } from './shared/desktopReliability'
import { getDesktopSecret } from './secrets'

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail: unknown) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

let cachedApiBase: string | null = null
let pendingDesktopApiBase: Promise<string> | null = null
let cachedDesktopApiToken: string | null | undefined
const responseEpochs = new WeakMap<Response, number>()
onDesktopEndpointChange(clearDesktopApiCache)

export function getConfiguredApiAddress(): string {
  return cachedApiBase?.replace(/^https?:\/\//, '') || '127.0.0.1'
}

export async function getApiBase(): Promise<string> {
  // Rust owns the endpoint. Check its epoch even when the previous port still accepts traffic.
  await getDesktopBackendStatus()
  if (cachedApiBase) return cachedApiBase
  if (!pendingDesktopApiBase) {
    const pending = waitForDesktopBackend()
      .then(status => {
        if (!desktopEndpointEpoch.matches(status)) throw new Error('本地核心端点已变化，请重试连接')
        cachedApiBase = `http://127.0.0.1:${status.port}`
        return cachedApiBase
      })
      .finally(() => { if (pendingDesktopApiBase === pending) pendingDesktopApiBase = null })
    pendingDesktopApiBase = pending
  }
  return pendingDesktopApiBase
}

export function clearDesktopApiCache(): void {
  cachedApiBase = null
  pendingDesktopApiBase = null
  cachedDesktopApiToken = undefined
}

async function getApiToken(): Promise<string | null> {
  if (cachedDesktopApiToken !== undefined) return cachedDesktopApiToken
  cachedDesktopApiToken = await invoke<string>('backend_api_token')
  return cachedDesktopApiToken
}

export function apiPathname(path: string): string {
  try {
    return new URL(path, 'http://siyi.local').pathname
  } catch {
    return path.split(/[?#]/, 1)[0]
  }
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const pathname = apiPathname(path)
  const optionHeaders = new Headers(options?.headers)
  const omitModelCredential = optionHeaders.get('X-Siyi-Omit-Model-Credential') === '1'
  optionHeaders.delete('X-Siyi-Omit-Model-Credential')
  const needsModelKey = needsModelCredential(path, omitModelCredential)
  const needsSearchKeys = pathname === '/api/chat' || pathname === '/api/tasks' || pathname.startsWith('/api/search/') || /\/api\/tasks\/[^/]+\/resume$/.test(pathname)
  const [desktopModelKey, tavilyKey, braveKey, apiToken] = await Promise.all([
    needsModelKey ? getDesktopSecret('model_api_key').catch(() => null) : Promise.resolve(null),
    needsSearchKeys ? getDesktopSecret('tavily_api_key').catch(() => null) : Promise.resolve(null),
    needsSearchKeys ? getDesktopSecret('brave_api_key').catch(() => null) : Promise.resolve(null),
    getApiToken(),
  ])
  // Resolve after credential reads so an intervening restart cannot pair a new epoch with an old port.
  const apiBase = await getApiBase()
  const headers = optionHeaders
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (desktopModelKey && !headers.has('X-Model-Api-Key')) headers.set('X-Model-Api-Key', desktopModelKey)
  if (tavilyKey && !headers.has('X-Tavily-Api-Key')) headers.set('X-Tavily-Api-Key', tavilyKey)
  if (braveKey && !headers.has('X-Brave-Api-Key')) headers.set('X-Brave-Api-Key', braveKey)
  if (apiToken) headers.set('X-Agent-Api-Token', apiToken)
  const endpointRevision = desktopEndpointEpoch.capture()
  try {
    const response = await fetch(`${apiBase}${path}`, {
      ...options,
      headers,
    })
    if (!desktopEndpointEpoch.accepts(endpointRevision)) throw new Error('本地核心已重启；旧请求结果已丢弃，请刷新后核对操作记录')
    responseEpochs.set(response, endpointRevision)
    return response
  } catch (error) {
    if (options?.signal?.aborted || (error as Error).name === 'AbortError') throw new DOMException('请求已取消', 'AbortError')
    clearDesktopApiCache()
    const msg = (error as Error).message || String(error)
    if (msg.includes('Failed to fetch') || msg.includes('NetworkError') || msg.includes('fetch')) {
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
  const value = await response.json()
  const revision = responseEpochs.get(response)
  if (revision !== undefined && !desktopEndpointEpoch.accepts(revision)) throw new Error('本地核心已重启；旧请求结果已丢弃，请刷新后核对操作记录')
  return value
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
  const endpointRevision = responseEpochs.get(response) ?? desktopEndpointEpoch.capture()
  const decoder = new TextDecoder()
  let buffer = ''
  let cursor = afterId
  while (true) {
    const { value, done } = await reader.read()
    if (signal.aborted) {
      await reader.cancel()
      throw new DOMException('任务流已取消', 'AbortError')
    }
    if (!desktopEndpointEpoch.accepts(endpointRevision)) {
      await reader.cancel()
      throw new Error('任务流所属核心已变化，请重新连接任务')
    }
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
