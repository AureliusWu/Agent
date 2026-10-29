import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

// Load production TypeScript/TSX in Node without adding a browser or test-only runtime dependency.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@tauri-apps/api/core') return { shortCircuit: true, url: 'data:text/javascript,export const invoke = (...args) => globalThis.desktopTestInvoke(...args)' }
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) {
        if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.css')) return { shortCircuit: true, format: 'module', source: 'export default {}' }
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) {
      return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText }
    }
    return nextLoad(url, context)
  },
})

globalThis.window = { __TAURI_INTERNALS__: {}, setTimeout }
let status = { epoch: 1, port: 43100, ready: true, phase: 'ready', pid: 100, restart_count: 0, error: null, log_directory: '' }
globalThis.desktopTestInvoke = async (command, args) => {
  if (command === 'backend_status') return { ...status }
  if (command === 'backend_api_token') return 'test-desktop-auth'
  if (command === 'get_secret') return args.name === 'model_api_key' ? 'synthetic-model-credential' : null
  throw new Error(`Unexpected native command: ${command}`)
}
const requests = []
let providerConfiguration = { provider_id: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash' }
globalThis.fetch = async (url, options) => {
  requests.push({ url, options })
  return new Response(JSON.stringify(url.endsWith('/api/provider/configuration') ? providerConfiguration : { status: 'ok' }), { headers: { 'Content-Type': 'application/json' } })
}
const { api, apiFetch, getApiBase, streamTaskEvents } = await import('../src/api.ts')
const { getDesktopBackendStatus } = await import('../src/desktopRuntime.ts')
await apiFetch('/api/provider/health?force=true')
assert.equal(requests.at(-1).options.headers.get('X-Model-Api-Key'), 'synthetic-model-credential', 'V15-UI-PROVIDER-HEALTH')
assert.match(requests.at(-1).options.headers.get('X-Model-Credential-Target'), /^model_api_key\.[a-f0-9]{64}$/)
assert.equal(requests.at(-1).options.headers.get('X-Model-Credential-Target').includes('synthetic-model-credential'), false)
await apiFetch('/api/provider/health?provider_id=ollama')
assert.equal(requests.at(-1).options.headers.has('X-Model-Api-Key'), false)
await apiFetch('/api/provider/health?force=true', { headers: { 'X-Siyi-Omit-Model-Credential': '1' } })
assert.equal(requests.at(-1).options.headers.has('X-Model-Api-Key'), false)
assert.equal(requests.at(-1).options.headers.has('X-Siyi-Omit-Model-Credential'), false)
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration) })
assert.equal(requests.at(-1).options.headers.get('X-Model-Api-Key'), 'synthetic-model-credential')
assert.match(requests.at(-1).options.headers.get('X-Model-Credential-Target'), /^model_api_key\.[a-f0-9]{64}$/, 'draft model directory checks bind to their explicit draft audience')
const normalInvoke = globalThis.desktopTestInvoke
let credentialReads = 0
globalThis.desktopTestInvoke = async (command, ...args) => {
  if (command === 'get_secret') { credentialReads++; throw new Error('synthetic private native error') }
  return normalInvoke(command, ...args)
}
const beforeCredentialFailure = requests.length
await assert.rejects(apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration) }), /无法读取 Windows 凭据管理器/)
assert.equal(requests.length, beforeCredentialFailure, 'credential read failure cannot send the directory request')
credentialReads = 0
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration), headers: { 'X-Model-Api-Key': 'synthetic-draft-key' } })
assert.equal(credentialReads, 0, 'explicit draft key does not read an unrelated saved credential')
assert.equal(requests.at(-1).options.headers.get('X-Model-Api-Key'), 'synthetic-draft-key')
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration), headers: { 'X-Siyi-Omit-Model-Credential': '1', 'X-Model-Api-Key': 'must-not-send' } })
assert.equal(requests.at(-1).options.headers.has('X-Model-Api-Key'), false, 'local omission fence also strips an explicit cloud header')
globalThis.desktopTestInvoke = normalInvoke
providerConfiguration = { ...providerConfiguration, provider_id: 'openai_compatible', base_url: 'https://another-provider.example/v1' }
const secretNames = []
globalThis.desktopTestInvoke = async (command, args) => {
  if (command === 'get_secret') { secretNames.push(args.name); return args.name === 'model_api_key' ? 'must-not-send-legacy' : null }
  return normalInvoke(command, args)
}
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration) })
assert.equal(secretNames.length, 1)
assert.match(secretNames[0], /^model_api_key\.[a-f0-9]{64}$/)
assert.equal(requests.at(-1).options.headers.has('X-Model-Api-Key'), false, 'a different remote target cannot use the original DeepSeek key')
providerConfiguration = { ...providerConfiguration, base_url: 'http://127.0.0.1:1234/v1' }
secretNames.length = 0
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(providerConfiguration), headers: { 'X-Model-Api-Key': 'must-not-send-even-without-omit-flag' } })
assert.equal(secretNames.length, 0, 'configured local targets do not read any model credential')
assert.equal(requests.at(-1).options.headers.has('X-Model-Api-Key'), false)
providerConfiguration = { provider_id: 'openai_compatible', base_url: 'https://current-provider-b.example/v1', model: 'b-model' }
secretNames.length = 0
const draftA = { provider_id: 'openai_compatible', base_url: 'https://draft-provider-a.example/v1', model: 'a-model' }
const { modelCredentialAlias } = await import('../src/shared/modelCredentials.ts')
await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(draftA) })
assert.deepEqual(secretNames, [await modelCredentialAlias(draftA)], 'a stale draft must read only its own target alias, never the latest saved provider key')
assert.equal(requests.at(-1).options.headers.get('X-Model-Credential-Target'), await modelCredentialAlias(draftA))
providerConfiguration = { provider_id: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash' }
globalThis.desktopTestInvoke = normalInvoke
status = { ...status, epoch: 2, port: 43200 }
assert.equal(await getApiBase(), 'http://127.0.0.1:43200', 'V15-UI-RESTART: cached port refreshes without a failed request')

let resolveMutation
let startMutation
const started = new Promise(resolve => { startMutation = resolve })
let mutationRequests = 0
globalThis.fetch = () => { mutationRequests += 1; startMutation(); return new Promise(resolve => { resolveMutation = resolve }) }
const mutation = apiFetch('/api/tools/execute', { method: 'POST', body: '{}' })
await started
status = { ...status, epoch: 3, port: 43300 }
await getDesktopBackendStatus()
resolveMutation(new Response('{"status":"ok"}'))
await assert.rejects(mutation, /旧请求结果已丢弃/)
assert.equal(mutationRequests, 1, 'a side-effect request must never replay automatically after restart')

let streamController
let streamStarted
const connected = new Promise(resolve => { streamStarted = resolve })
globalThis.fetch = async () => {
  streamStarted()
  return new Response(new ReadableStream({ start(controller) { streamController = controller } }))
}
let receivedEvents = 0
const stream = streamTaskEvents('test-task', () => { receivedEvents += 1 }, new AbortController().signal)
await connected
await new Promise(resolve => setTimeout(resolve, 0))
status = { ...status, epoch: 4, port: 43400 }
await getDesktopBackendStatus()
streamController.enqueue(new TextEncoder().encode('data: {"id":1,"task_id":"test-task","event":"model.delta","payload":{"delta":"old"}}\n\n'))
await assert.rejects(stream, /任务流所属核心已变化/)
assert.equal(receivedEvents, 0, 'old-sidecar SSE cannot update the new epoch')

// Readiness is a liveness observation, not a new sidecar generation. A slow health
// probe must not discard an authenticated response or replay a completed mutation.
const stableEndpoint = { epoch: status.epoch, port: status.port }
async function observeReadiness(ready) {
  status = { ...status, ready, phase: ready ? 'ready' : 'error' }
  const observed = await getDesktopBackendStatus()
  assert.equal(observed.ready, ready)
  assert.deepEqual({ epoch: observed.epoch, port: observed.port }, stableEndpoint)
}

let resolveSlowRead
let startSlowRead
const slowReadStarted = new Promise(resolve => { startSlowRead = resolve })
let slowReadRequests = 0
globalThis.fetch = () => {
  slowReadRequests += 1
  startSlowRead()
  return new Promise(resolve => { resolveSlowRead = resolve })
}
const slowRead = api('/api/health')
await slowReadStarted
await observeReadiness(false)
await observeReadiness(true)
resolveSlowRead(new Response('{"status":"ok","same_generation":true}'))
assert.deepEqual(await slowRead, { status: 'ok', same_generation: true }, 'same-generation readiness recovery preserves a pending GET response')
assert.equal(slowReadRequests, 1, 'a transient readiness change does not replay a GET')

let resolveSlowMutation
let startSlowMutation
const slowMutationStarted = new Promise(resolve => { startSlowMutation = resolve })
let slowMutationRequests = 0
globalThis.fetch = () => {
  slowMutationRequests += 1
  startSlowMutation()
  return new Promise(resolve => { resolveSlowMutation = resolve })
}
const slowMutation = api('/api/tools/execute', { method: 'POST', body: '{}' })
await slowMutationStarted
await observeReadiness(false)
await observeReadiness(true)
resolveSlowMutation(new Response('{"status":"ok","operation_id":"same-generation-write"}'))
assert.deepEqual(await slowMutation, { status: 'ok', operation_id: 'same-generation-write' }, 'same-generation readiness recovery preserves a pending POST result')
assert.equal(slowMutationRequests, 1, 'a side-effect request must never replay after a readiness change')

let readinessStreamController
let startReadinessStream
const readinessStreamConnected = new Promise(resolve => { startReadinessStream = resolve })
let readinessStreamRequests = 0
globalThis.fetch = async () => {
  readinessStreamRequests += 1
  startReadinessStream()
  return new Response(new ReadableStream({ start(controller) { readinessStreamController = controller } }))
}
const readinessEvents = []
const readinessStream = streamTaskEvents('readiness-task', event => { readinessEvents.push(event) }, new AbortController().signal)
await readinessStreamConnected
await new Promise(resolve => setTimeout(resolve, 0))
await observeReadiness(false)
await observeReadiness(true)
readinessStreamController.enqueue(new TextEncoder().encode('data: {"id":7,"task_id":"readiness-task","event":"model.delta","payload":{"delta":"current"}}\n\n'))
readinessStreamController.close()
assert.equal(await readinessStream, 7, 'same-generation readiness recovery preserves the SSE cursor')
assert.deepEqual(readinessEvents, [{ id: 7, task_id: 'readiness-task', event: 'model.delta', payload: { delta: 'current' } }])
assert.equal(readinessStreamRequests, 1, 'a readiness change must not reconnect or replay the task stream')

const { FileOperationsPanel } = await import('../src/components/workspace/FileOperationsPanel.tsx')
const readonly = renderToStaticMarkup(createElement(FileOperationsPanel, { conversationId: 1, workspace: 'test-workspace', mode: 'readonly', selected: '', onChanged() {} }))
assert.match(readonly, /只读 · 写入和撤销均被禁用/)
assert.match(readonly, /<button[^>]*disabled=""[^>]*>加入计划并预览差异<\/button>/, 'V15-UI-PERMISSION-DIALOG: readonly has no enabled mutation control')
assert.match(readonly, /撤销时间线/)
const { FilesPanel } = await import('../src/components/workspace/FilesPanel.tsx')
const empty = renderToStaticMarkup(createElement(FilesPanel, { active: null, workspace: '', mode: 'ask' }))
assert.match(empty, /请先选择一个工作区对话/)
assert.doesNotMatch(empty, /aria-label="文件操作"/)
console.log('Production API/sidecar restart/readiness/SSE and Files component tests passed')
const { TopHeader } = await import('../src/components/shared/TopHeader.tsx')
const header = renderToStaticMarkup(createElement(TopHeader, { sidebarExpanded: false, onToggleSidebar() {}, onToggleSummary() {}, onNewConversation() {}, onNavigate() {} }))
for (const label of ['打开侧边栏', '新建会话', '最小化', '最大化', '关闭窗口']) assert.ok(header.includes(`aria-label="${label}"`), `custom titlebar retains ${label}`)
assert.match(header, />应用<\/button>/)
assert.match(header, />编辑<\/button>/)
assert.match(header, /data-tauri-drag-region/)
const desktopConfig = JSON.parse(readFileSync(new URL('../../src-tauri/tauri.conf.json', import.meta.url), 'utf8'))
assert.equal(desktopConfig.app.windows[0].decorations, false)
const windowPermissions = JSON.parse(readFileSync(new URL('../../src-tauri/capabilities/default.json', import.meta.url), 'utf8')).permissions
for (const action of ['minimize', 'toggle-maximize', 'close', 'start-dragging']) assert.ok(windowPermissions.includes(`core:window:allow-${action}`))
assert.equal(windowPermissions.includes('core:window:allow-all'), false, 'custom chrome must not add blanket window access')
await import('./window-chrome.test.ts')
