import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Exercise the production apiFetch, destination helpers and native-secret facade.
// Only the native bridge and HTTP transport are synthetic; no actual key is read.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@tauri-apps/api/core') return { shortCircuit: true, url: 'data:text/javascript,export const invoke = (...args) => globalThis.credentialRoutingInvoke(...args)' }
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText }
    return nextLoad(url, context)
  },
})

globalThis.window = { __TAURI_INTERNALS__: {}, setTimeout }
let status = { epoch: 1, port: 43100, ready: true, phase: 'ready', pid: 100, restart_count: 0, error: null, log_directory: '' }
let configuration = { provider_id: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash' }
const remote = { ...configuration }
const secretReads = []
const requests = []
let failConfiguration = false
let replaceCoreDuringConfiguration = false
globalThis.credentialRoutingInvoke = async (command, args) => {
  if (command === 'backend_status') return { ...status }
  if (command === 'backend_api_token') return 'synthetic-desktop-auth'
  if (command === 'get_secret') {
    secretReads.push(args.name)
    return { model_api_key: 'synthetic-model-credential', tavily_api_key: 'synthetic-tavily-credential', brave_api_key: 'synthetic-brave-credential' }[args.name] ?? null
  }
  throw new Error(`Unexpected native command: ${command}`)
}
globalThis.fetch = async (url, options) => {
  requests.push({ path: new URL(url).pathname, options })
  if (url.endsWith('/api/provider/configuration')) {
    if (failConfiguration) return new Response('{}', { status: 503 })
    if (replaceCoreDuringConfiguration) {
      status = { ...status, epoch: status.epoch + 1, port: status.port + 1 }
      await getDesktopBackendStatus()
    }
    return new Response(JSON.stringify(configuration), { headers: { 'Content-Type': 'application/json' } })
  }
  return new Response('{"status":"ok"}', { headers: { 'Content-Type': 'application/json' } })
}
const { apiFetch } = await import('../src/api.ts')
const { getDesktopBackendStatus } = await import('../src/desktopRuntime.ts')
const { modelCredentialAlias } = await import('../src/shared/modelCredentials.ts')
const { needsModelCredential } = await import('../src/shared/desktopReliability.ts')
const reset = () => { secretReads.length = 0; requests.length = 0 }
const headers = () => requests.at(-1).options.headers
const post = { method: 'POST', body: '{}' }

// Merely loading/reading task history is not a request to resolve cloud secrets.
for (const [path, method] of [['/api/tasks', 'GET'], ['/api/tasks?conversation_id=7', 'GET'], ['/api/tasks', 'HEAD'], ['/api/tasks/synthetic-task', 'GET'], ['/api/tasks/synthetic-task/events', 'GET'], ['/api/tasks/synthetic-task/resume', 'GET'], ['/api/provider/health?provider_id=mock', 'GET']]) {
  reset()
  await apiFetch(path, { method })
  assert.deepEqual(secretReads, [], `${method} ${path} must not access Credential Manager`)
  assert.equal(requests.length, 1, 'read-only history must not fetch model configuration just to obtain keys')
  assert.equal(headers().has('X-Model-Api-Key'), false)
  assert.equal(headers().has('X-Tavily-Api-Key'), false)
  assert.equal(headers().has('X-Brave-Api-Key'), false)
}
assert.equal(needsModelCredential('/api/tasks'), false)
assert.equal(needsModelCredential('/api/tasks', false, 'POST'), true)
assert.equal(needsModelCredential('/api/tasks/synthetic-task/resume', false, 'POST'), true)
assert.equal(needsModelCredential('/api/conversations/7/compact', false, 'POST'), true)
assert.equal(needsModelCredential('/api/conversations/7/compact'), false)
assert.equal(needsModelCredential('/api/provider/health?force=true'), true)

// Cloud execution retains model+search credentials, but only after confirming
// its current destination. Lower-case HTTP methods are normalized as well.
for (const path of ['/api/tasks', '/api/chat', '/api/tasks/synthetic-task/resume']) {
  configuration = { ...remote }; reset()
  await apiFetch(path, { ...post, method: 'post' })
  assert.equal(requests.filter(request => request.path === '/api/provider/configuration').length, 1)
  assert.deepEqual(new Set(secretReads), new Set([await modelCredentialAlias(remote), 'model_api_key', 'tavily_api_key', 'brave_api_key']))
  assert.equal(headers().get('X-Model-Api-Key'), 'synthetic-model-credential')
  assert.equal(headers().get('X-Model-Credential-Target'), await modelCredentialAlias(remote))
  assert.equal(headers().get('X-Tavily-Api-Key'), 'synthetic-tavily-credential')
  assert.equal(headers().get('X-Brave-Api-Key'), 'synthetic-brave-credential')
}

// Local tasks neither read nor carry cloud credentials, including explicit
// caller-provided headers. The server remains the authoritative local fence.
for (const local of [{ provider_id: 'mock', base_url: '', model: '' }, { provider_id: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'synthetic-model' }, { provider_id: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1', model: 'synthetic-model' }, { provider_id: 'openai_compatible', base_url: 'http://[::1]:1234/v1', model: 'synthetic-model' }]) {
  for (const path of ['/api/tasks', '/api/chat', '/api/tasks/synthetic-task/resume']) {
    configuration = local; reset()
    await apiFetch(path, { ...post, headers: { 'X-Model-Api-Key': 'must-not-send', 'X-Tavily-Api-Key': 'must-not-send', 'X-Brave-Api-Key': 'must-not-send', 'X-Model-Credential-Target': 'must-not-send' } })
    assert.deepEqual(secretReads, [], `${local.provider_id} ${local.base_url} must not access cloud secrets`)
    for (const header of ['X-Model-Api-Key', 'X-Tavily-Api-Key', 'X-Brave-Api-Key', 'X-Model-Credential-Target']) assert.equal(headers().has(header), false, header)
  }
}

// Explicit search configuration/health operations retain their own credentials
// even if model inference is local; they never read a model credential.
configuration = { provider_id: 'mock', base_url: '', model: '' }
for (const [path, method] of [['/api/search/providers', 'GET'], ['/api/search/providers/tavily/health', 'POST'], ['/api/search/compare?query=synthetic', 'POST']]) {
  reset(); await apiFetch(path, { method })
  assert.deepEqual(new Set(secretReads), new Set(['tavily_api_key', 'brave_api_key']))
  assert.equal(requests.length, 1)
  assert.equal(headers().has('X-Model-Api-Key'), false)
}

configuration = { ...remote }; reset()
await apiFetch('/api/tasks', { ...post, headers: { 'X-Model-Api-Key': 'explicit-model', 'X-Tavily-Api-Key': 'explicit-tavily', 'X-Brave-Api-Key': 'explicit-brave' } })
assert.deepEqual(secretReads, [], 'explicit destination-bound credentials need no native reads')
assert.equal(headers().get('X-Model-Api-Key'), 'explicit-model')
assert.equal(headers().get('X-Model-Credential-Target'), await modelCredentialAlias(remote))
assert.equal(headers().get('X-Tavily-Api-Key'), 'explicit-tavily')

// Draft model checks remain bound to the draft audience, not saved configuration.
const draft = { ...remote, provider_id: 'openai_compatible', base_url: 'https://draft.example/v1', model: 'draft-model' }
reset(); await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify(draft) })
assert.deepEqual(secretReads, [await modelCredentialAlias(draft)])
assert.equal(headers().get('X-Model-Credential-Target'), await modelCredentialAlias(draft))
assert.equal(requests.some(request => request.path === '/api/provider/configuration'), false)
reset(); await apiFetch('/api/provider/models', { method: 'POST', body: JSON.stringify({ ...draft, base_url: 'http://localhost:1234/v1' }), headers: { 'X-Model-Api-Key': 'must-not-send' } })
assert.deepEqual(secretReads, [])
assert.equal(headers().has('X-Model-Api-Key'), false)

configuration = { ...remote }; failConfiguration = true; reset()
await assert.rejects(apiFetch('/api/tasks', post), /无法确认当前模型提供商/)
assert.deepEqual(secretReads, [])
assert.equal(requests.every(request => request.path === '/api/provider/configuration'), true)
failConfiguration = false; replaceCoreDuringConfiguration = true; reset()
await assert.rejects(apiFetch('/api/tasks', post), /本地核心已变化/)
assert.deepEqual(secretReads, [])
assert.equal(requests.every(request => request.path === '/api/provider/configuration'), true)
console.log('Production API credential routing: read-only tasks, local tasks, remote execution, explicit searches, draft audiences and stale-core fences passed')
