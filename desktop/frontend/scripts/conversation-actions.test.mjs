import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Mount the production App with synthetic HTTP, storage and React scheduling.
// No WebView, native dialog, real workspace, credential or network is used.
const stub = source => ({ shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` })
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return stub('export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";')
    if (specifier === 'react') return stub(['useState', 'useRef', 'useEffect', 'useMemo'].map(name => `export const ${name}=(...args)=>globalThis.conversationActionsTest.hooks.${name}(...args);`).join(''))
    if (specifier === 'lucide-react') return stub('export const ArrowLeft="ArrowLeft";')
    if (context.parentURL?.endsWith('/App.tsx')) {
      if (specifier === './api') return stub('export const api=(...args)=>globalThis.conversationActionsTest.api(...args);export const apiFetch=(...args)=>globalThis.conversationActionsTest.apiFetch(...args);export const clearDesktopApiCache=()=>{};export const getApiBase=async()=>"http://127.0.0.1";export const getConfiguredApiAddress=()=>"127.0.0.1";')
      if (specifier === './desktopRuntime') return stub('export const getDesktopBackendStatus=async()=>({ready:true});export const restartDesktopBackend=async()=>({ready:true});')
      if (specifier === './hooks/useAgentChat') return stub('export const useAgentChat=()=>globalThis.conversationActionsTest.chat;')
      if (specifier === './hooks/useMediaQuery') return stub('export const useMediaQuery=()=>false;')
      if (specifier === './buildInfo') return stub('export const useBuildInfo=()=>({environment:"Synthetic",version:"test"});')
      if (specifier.startsWith('./components/')) { const name = specifier.endsWith('/VoiceChatContext') ? 'VoiceChatProvider' : specifier.split('/').at(-1); return stub(`export const ${name}="${name}";`) }
    }
    if (specifier.endsWith('.css')) return stub('export default {};')
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
const { default: App } = await import('../src/App.tsx')
const A = { id: 1, title: 'Synthetic A', workspace: 'workspace-a', permission_mode: 'ask', agent_profile_id: 'general' }
const B = { id: 2, title: 'Synthetic B', workspace: 'workspace-b', permission_mode: 'readonly', agent_profile_id: 'general' }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))

function harness(custom = () => undefined) {
  const slots = [], effects = [], calls = [], errors = [], storage = new Map([['agent.activeConversationId', '1']])
  let cursor = 0, dirty = true, tree, disposed = false, resets = 0
  const h = {
    calls, errors, storage,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, value => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; dirty = true }] },
      useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
      useMemo(factory, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { deps, value: factory() }; return slots[index].value },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) { const previous = slots[index]; slots[index] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = effect() }) } },
    },
    chat: { messages: [], pending: [], runtimeEvents: [], queued: [], commands: [], setError(message) { errors.push(message) }, resetConversation() { resets++ } },
    async api(url, options) {
      const body = options?.body ? JSON.parse(options.body) : undefined
      calls.push({ url, method: options?.method || 'GET', body })
      const result = custom(url, options?.method || 'GET', body)
      if (result !== undefined) return result
      if (url === '/api/health') return { status: 'ok' }
      if (url === '/api/provider/policy') return { provider: { default_model: 'synthetic', models: [] }, models: {} }
      if (url === '/api/conversations' && !options?.method) return [{ ...A }, { ...B }]
      return { status: 'ok' }
    },
    async apiFetch() { assert.fail('Unexpected upload request') },
    render() { globalThis.conversationActionsTest = h; if (dirty && !disposed) { dirty = false; cursor = 0; tree = App(); for (const effect of effects.splice(0)) effect() } return tree },
    component(name) { const node = nodes(h.render()).find(node => node.type === name); assert.ok(node, `component ${name}`); return node.props },
    async select(item) { await h.component('CollapsibleSidebar').onSelect({ ...item }); await h.flush() },
    snapshot() { return { active: h.component('CollapsibleSidebar').active, conversations: h.component('CollapsibleSidebar').conversations, mode: h.component('ChatView').mode, workspace: h.component('ChatView').workspace, resets } },
    async flush() { for (let index = 0; index < 8; index++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { disposed = true; for (const slot of slots) slot?.cleanup?.() },
  }
  globalThis.conversationActionsTest = h
  globalThis.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) }
  globalThis.window = { addEventListener() {}, removeEventListener() {} }
  globalThis.prompt = () => 'Renamed synthetic A'
  globalThis.confirm = () => true
  h.render()
  return h
}

let failures = 0
async function test(name, check) { try { await check(); console.log(`PASS ${name}`) } catch (error) { failures++; console.error(`FAIL ${name}: ${error.message}`) } }
await test('late permission success cannot select the old conversation', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'PATCH' && url.endsWith('/permission') ? pending.promise : undefined)
  await h.flush(); const save = h.component('ChatView').onMode('full'); await h.flush(); await h.select(B)
  pending.resolve({ status: 'ok' }); await save; await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.equal(h.snapshot().mode, 'readonly'); assert.equal(h.storage.get('agent.permissionMode'), 'readonly')
  h.dispose()
})
await test('late permission failure cannot roll back a different conversation', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'PATCH' && url.endsWith('/permission') ? pending.promise : undefined)
  await h.flush(); const save = h.component('ChatView').onMode('full'); await h.flush(); await h.select(B)
  pending.reject(new Error('synthetic save failure')); await save; await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.equal(h.snapshot().mode, 'readonly'); assert.equal(h.storage.get('agent.permissionMode'), 'readonly'); assert.equal(h.errors.filter(Boolean).length, 0)
  h.dispose()
})
await test('late rename updates its row without stealing active session', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'PATCH' && url === '/api/conversations/1' ? pending.promise : undefined)
  await h.flush(); const rename = h.component('CollapsibleSidebar').onRename(A); await h.select(B)
  pending.resolve({}); await rename; await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.equal(h.snapshot().conversations.find(item => item.id === A.id).title, 'Renamed synthetic A')
  h.dispose()
})
await test('late delete cannot clear another active conversation', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'DELETE' ? pending.promise : undefined)
  await h.flush(); const remove = h.component('CollapsibleSidebar').onDelete(A); await h.select(B)
  pending.resolve({}); await remove; await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.equal(h.storage.get('agent.activeConversationId'), '2'); assert.equal(h.snapshot().resets, 0)
  assert.equal(h.snapshot().conversations.some(item => item.id === A.id), false)
  h.dispose()
})
await test('late creation preserves new user selection while retaining created row', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'POST' && url === '/api/conversations' ? pending.promise : undefined)
  await h.flush(); h.component('TopHeader').onNewConversation(); await h.select(B)
  pending.resolve({ ...A, id: 3, title: 'Created late' }); await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.ok(h.snapshot().conversations.some(item => item.id === 3)); assert.equal(h.snapshot().mode, B.permission_mode)
  h.dispose()
})
await test('late startup restoration cannot overwrite a user selection', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'GET' && url === '/api/conversations' ? pending.promise : undefined)
  await h.flush(); await h.select(B); pending.resolve([A, B]); await h.flush()
  assert.equal(h.snapshot().active.id, B.id); assert.equal(h.snapshot().workspace, B.workspace); assert.equal(h.snapshot().mode, B.permission_mode)
  h.dispose()
})
await test('permission saves serialize per conversation and roll back to last confirmed value', async () => {
  const first = deferred(), second = deferred(); let writes = 0
  const h = harness((url, method) => method === 'PATCH' && url.endsWith('/permission') ? (++writes === 1 ? first.promise : second.promise) : undefined)
  await h.flush(); const saveFirst = h.component('ChatView').onMode('full'); await h.flush()
  const saveSecond = h.component('ChatView').onMode('readonly'); await h.flush()
  assert.equal(writes, 1, 'the second request must wait instead of racing its predecessor')
  first.resolve({}); await saveFirst; await h.flush()
  assert.equal(writes, 2); assert.equal(h.snapshot().mode, 'readonly', 'an intermediate completion cannot rewind the newer optimistic choice')
  second.reject(new Error('second synthetic permission failure')); await saveSecond; await h.flush()
  assert.equal(h.snapshot().mode, 'full'); assert.equal(h.snapshot().active.permission_mode, 'full'); assert.equal(h.storage.get('agent.permissionMode'), 'full')
  assert.match(h.errors.at(-1), /second synthetic permission failure/)
  h.dispose()
})
await test('a failed superseded permission save does not roll back the latest pending choice', async () => {
  const first = deferred(), second = deferred(); let writes = 0
  const h = harness((url, method) => method === 'PATCH' && url.endsWith('/permission') ? (++writes === 1 ? first.promise : second.promise) : undefined)
  await h.flush(); const saveFirst = h.component('ChatView').onMode('full'); await h.flush()
  const saveSecond = h.component('ChatView').onMode('readonly'); await h.flush()
  first.reject(new Error('superseded synthetic failure')); await saveFirst; await h.flush()
  assert.equal(h.snapshot().mode, 'readonly'); assert.equal(h.errors.filter(Boolean).length, 0)
  second.resolve({}); await saveSecond; await h.flush()
  assert.equal(h.snapshot().mode, 'readonly'); assert.equal(h.snapshot().active.permission_mode, 'readonly')
  h.dispose()
})
await test('parallel rename and permission completion preserve both metadata changes', async () => {
  const renamePending = deferred(), modePending = deferred()
  const h = harness((url, method) => method !== 'PATCH' ? undefined : url.endsWith('/permission') ? modePending.promise : renamePending.promise)
  await h.flush(); const save = h.component('ChatView').onMode('full'); const rename = h.component('CollapsibleSidebar').onRename(A); await h.flush()
  renamePending.resolve({}); await rename; await h.flush(); modePending.resolve({}); await save; await h.flush()
  assert.equal(h.snapshot().active.title, 'Renamed synthetic A'); assert.equal(h.snapshot().active.permission_mode, 'full')
  h.dispose()
})
await test('out-of-order new conversations cannot focus an older creation intent', async () => {
  const first = deferred(), second = deferred(); let creates = 0
  const h = harness((url, method) => method === 'POST' && url === '/api/conversations' ? (++creates === 1 ? first.promise : second.promise) : undefined)
  await h.flush(); h.component('TopHeader').onNewConversation(); h.component('TopHeader').onNewConversation()
  second.resolve({ ...B, id: 4 }); await h.flush(); first.resolve({ ...A, id: 3 }); await h.flush()
  assert.equal(h.snapshot().active.id, 4); assert.equal(h.snapshot().mode, B.permission_mode)
  assert.ok(h.snapshot().conversations.some(item => item.id === 3)); assert.ok(h.snapshot().conversations.some(item => item.id === 4))
  h.dispose()
})
await test('old list refresh cannot erase a conversation created while it was in flight', async () => {
  const pending = deferred(), h = harness((url, method) => method === 'GET' && url === '/api/conversations' ? pending.promise : method === 'POST' ? { ...A, id: 3 } : undefined)
  await h.flush(); h.component('TopHeader').onNewConversation(); await h.flush(); pending.resolve([A, B]); await h.flush()
  assert.equal(h.snapshot().active.id, 3); assert.ok(h.snapshot().conversations.some(item => item.id === 3))
  h.dispose()
})
await test('failed creation does not discard an otherwise valid initial conversation list', async () => {
  const listing = deferred(), creation = deferred()
  const h = harness((url, method) => url !== '/api/conversations' ? undefined : method === 'GET' ? listing.promise : creation.promise)
  await h.flush(); h.component('TopHeader').onNewConversation(); creation.reject(new Error('synthetic create failed')); await h.flush()
  listing.resolve([A, B]); await h.flush()
  assert.deepEqual(h.snapshot().conversations.map(item => item.id), [A.id, B.id]); assert.equal(h.snapshot().active, null)
  h.dispose()
})
await test('late upload approval cannot prompt or write again after switching conversation', async () => {
  const pending = deferred(), h = harness(); let uploads = 0, confirmations = 0
  h.apiFetch = async () => { uploads++; return { ok: true, json: () => pending.promise } }
  globalThis.confirm = () => { confirmations++; return true }
  await h.flush(); const upload = h.component('ChatView').onUploadFile(new File(['synthetic'], 'fixture.txt', { type: 'text/plain' })); await h.select(B)
  pending.resolve({ status: 'confirmation_required', approval_key: 'synthetic-once' }); await upload; await h.flush()
  assert.equal(uploads, 1); assert.equal(confirmations, 0); assert.equal(h.snapshot().active.id, B.id)
  h.dispose()
})
await test('idle permission queue accepts a newly observed persisted baseline', async () => {
  const { ConversationPermissionQueue } = await import('../src/shared/conversationActions.ts')
  const queue = new ConversationPermissionQueue()
  await queue.save(A.id, 'ask', 'full', async () => {})
  const result = await queue.save(A.id, 'readonly', 'agent', async () => { throw new Error('synthetic later write failed') })
  assert.equal(result.ok, false); assert.equal(result.mode, 'readonly', 'a fresh server-observed mode replaces an old idle baseline')
})
if (failures) throw new Error(`${failures} conversation action race regressions failed`)
console.log('Production App conversation action race tests passed; no native UI, real data or model calls')
