import { invoke } from '@tauri-apps/api/core'
import { desktopEndpointEpoch, getDesktopBackendStatus, onDesktopEndpointChange, waitForDesktopBackend } from './desktopRuntime'
import { isTaskExecution, needsModelCredential } from './shared/desktopReliability'
import { getDesktopSecret } from './secrets'
import { isLocalModelProvider, mayUseLegacyModelCredential, modelCredentialAlias } from './shared/modelCredentials'
import type { ProviderConfiguration } from './types'

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

type ModelCredential = { key: string | null; target: string | null; revision: number }

async function credentialForConfiguration(configuration: ProviderConfiguration, revision: number, readKey: boolean): Promise<ModelCredential> {
  if (isLocalModelProvider(configuration)) return { key: null, target: null, revision }
  const target = await modelCredentialAlias(configuration)
  if (!readKey) return { key: null, target, revision }
  try {
    const key = await getDesktopSecret(target)
    if (key) return { key, target, revision }
    // Preserve the original entry in place; only the official DeepSeek target can use it.
    return { key: mayUseLegacyModelCredential(configuration) ? await getDesktopSecret('model_api_key') : null, target, revision }
  } catch {
    throw new Error('无法读取 Windows 凭据管理器中的模型 API Key。密钥未被删除，请检查当前 Windows 账户后重试。')
  }
}

function modelDirectoryDraft(body: BodyInit | null | undefined): ProviderConfiguration {
  try {
    if (typeof body !== 'string') throw new Error('invalid body')
    const configuration = JSON.parse(body) as ProviderConfiguration
    if (!['deepseek', 'ollama', 'openai_compatible', 'mock'].includes(configuration.provider_id) || typeof configuration.base_url !== 'string' || typeof configuration.model !== 'string') throw new Error('invalid configuration')
    return configuration
  } catch { throw new Error('获取模型需要完整的当前提供商配置，已停止读取和发送 API Key。') }
}

async function configuredProviderSnapshot(apiToken: string | null): Promise<{ configuration: ProviderConfiguration; revision: number }> {
  // Configuration is non-secret and is fetched without the credential-injecting API
  // wrapper. The destination is resolved before selecting a Credential Manager alias.
  const base = await getApiBase()
  const revision = desktopEndpointEpoch.capture()
  const response = await fetch(`${base}/api/provider/configuration`, { headers: apiToken ? { 'X-Agent-Api-Token': apiToken } : {} })
  if (!response.ok) throw new Error('无法确认当前模型提供商，已停止读取和发送 API Key。')
  const configuration = await response.json() as ProviderConfiguration
  if (!desktopEndpointEpoch.accepts(revision)) throw new Error('本地核心已变化，已停止读取和发送 API Key。')
  return { configuration, revision }
}

export async function apiFetch(path: string, options?: RequestInit): Promise<Response> {
  const pathname = apiPathname(path)
  const method = (options?.method || 'GET').toUpperCase()
  const directoryDraft = pathname === '/api/provider/models' ? modelDirectoryDraft(options?.body) : null
  const optionHeaders = new Headers(options?.headers)
  const omitModelCredential = optionHeaders.get('X-Siyi-Omit-Model-Credential') === '1' || Boolean(directoryDraft && isLocalModelProvider(directoryDraft))
  optionHeaders.delete('X-Siyi-Omit-Model-Credential')
  optionHeaders.delete('X-Model-Credential-Target')
  if (omitModelCredential) optionHeaders.delete('X-Model-Api-Key')
  const needsModelKey = needsModelCredential(path, omitModelCredential, method)
  const taskExecution = isTaskExecution(path, method)
  const apiToken = await getApiToken()
  await getApiBase()
  // Resolve the current destination once before any native credential access.
  // History reads never need this lookup; local tasks need no cloud secrets.
  const providerSnapshot = directoryDraft
    ? { configuration: directoryDraft, revision: desktopEndpointEpoch.capture() }
    : needsModelKey || taskExecution ? await configuredProviderSnapshot(apiToken) : null
  const localModelRequest = Boolean(providerSnapshot && isLocalModelProvider(providerSnapshot.configuration))
  if (localModelRequest) optionHeaders.delete('X-Model-Api-Key')
  if (taskExecution && localModelRequest) {
    optionHeaders.delete('X-Tavily-Api-Key')
    optionHeaders.delete('X-Brave-Api-Key')
  }
  const needsSearchKeys = pathname.startsWith('/api/search/') || (taskExecution && !localModelRequest)
  const [modelCredential, tavilyKey, braveKey] = await Promise.all([
    needsModelKey && providerSnapshot ? credentialForConfiguration(providerSnapshot.configuration, providerSnapshot.revision, !optionHeaders.has('X-Model-Api-Key')) : Promise.resolve(null),
    needsSearchKeys && !optionHeaders.has('X-Tavily-Api-Key') ? getDesktopSecret('tavily_api_key').catch(() => null) : Promise.resolve(null),
    needsSearchKeys && !optionHeaders.has('X-Brave-Api-Key') ? getDesktopSecret('brave_api_key').catch(() => null) : Promise.resolve(null),
  ])
  // Resolve after credential reads so an intervening restart cannot pair a new epoch with an old port.
  const apiBase = await getApiBase()
  if (providerSnapshot && !desktopEndpointEpoch.accepts(providerSnapshot.revision)) throw new Error('本地核心已变化，已停止发送旧提供商的 API Key。')
  const headers = optionHeaders
  if (!(options?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (modelCredential?.key && !headers.has('X-Model-Api-Key')) headers.set('X-Model-Api-Key', modelCredential.key)
  if (modelCredential?.target) headers.set('X-Model-Credential-Target', modelCredential.target)
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
