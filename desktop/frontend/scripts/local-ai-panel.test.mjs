import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Execute the production component with synthetic HTTP and React scheduling.
// No native microphone, model, user file or application is contacted.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect'].map(name => `export const ${name}=(...args)=>globalThis.localAiTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['Check', 'Cpu', 'Download', 'Mic', 'Play', 'RefreshCw', 'Square', 'Trash2', 'Volume2'].map(name => `export const ${name}="${name}";`).join(''))}` }
    if (specifier === '../../api') return { shortCircuit: true, url: 'data:text/javascript,export const api=(...args)=>globalThis.localAiTest.api(...args);export const apiFetch=api;' }
    if (specifier === './MicrophoneSettingsControl') return { shortCircuit: true, url: 'data:text/javascript,export const MicrophoneSettingsControl="SyntheticMicrophoneSettingsControl";' }
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
const { LocalAiPanel } = await import('../src/components/providers/LocalAiPanel.tsx')
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]))
const qualification = (status = 'NOT_RUN', model = 'synthetic-model') => ({
  effective_capabilities: { model, context_window_tokens: 1024, model_digest: 'a'.repeat(64), window_status: 'configured' },
  qualification: { status, protocol: 'runtime-file-v1', target_version: '16.0.0', run_id: null, finished_at: null, trust: 'synthetic-test-only', levels: Object.fromEntries(['basic', 'readonly_tools', 'structured_plan', 'file_agent'].map(key => [key, { qualified: status === 'PASS', status, reasons: [] }])) },
})
function harness(custom = () => undefined) {
  const slots = [], effects = [], calls = [], timers = new Map()
  let cursor = 0, dirty = true, tree, disposed = false, timerId = 0, lateUpdates = 0
  const h = {
    calls, timers,
    hooks: {
      useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[i].value, value => { if (disposed) lateUpdates++; slots[i].value = typeof value === 'function' ? value(slots[i].value) : value; dirty = true }] },
      useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i] },
      useEffect(effect, deps) { const i = cursor++; if (!slots[i] || !sameDeps(slots[i].deps, deps)) { const old = slots[i]; slots[i] = { deps, cleanup: old?.cleanup }; effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = effect() }) } },
    },
    async api(url, options) {
      const body = options?.body ? JSON.parse(options.body) : undefined
      calls.push({ url, method: options?.method || 'GET', body })
      const special = custom(url, body, options)
      if (special !== undefined) return special
      const defaults = {
        '/api/local-models/service': { status: 'INSTALLED_STOPPED', mode: 'synthetic' },
        '/api/local-models/models': [], '/api/provider/configuration': { provider_id: 'ollama', model: 'synthetic-model', max_tokens: 32 },
        '/api/tts/settings': null, '/api/tts/voices': [], '/api/local-models/resources': { snapshot: { system_available_bytes: 0, gpu_free_bytes: null, ollama_rss_bytes: null, sampled_at: new Date().toISOString() }, policy: { minimum_available_ram_bytes: 2147483648 } },
        '/api/stt/settings': { enabled: true, provider: 'faster_whisper', model_id: 'small', device: 'cpu', compute_type: 'int8', vad: true, idle_unload_minutes: 5, gpu_experimental: false },
        '/api/stt/models': [], '/api/stt/health': { providers: [], worker: { pid: null } }, '/api/stt/status': { status: 'IDLE', loaded_model: null, worker_pid: null },
        '/api/local-models/qualification': qualification(), '/api/local-models/qualification/import': qualification('PASS'),
      }
      return Object.hasOwn(defaults, url) ? defaults[url] : { status: 'ok' }
    },
    render() { globalThis.localAiTest = h; if (dirty && !disposed) { dirty = false; cursor = 0; tree = LocalAiPanel(); for (const effect of effects.splice(0)) effect() } return tree },
    text() { return words(h.render()) },
    button(label) { const value = nodes(h.render()).find(node => node.type === 'button' && (words(node) === label || node.props.title === label)); assert.ok(value, `button ${label}`); return value },
    upload(file) { nodes(h.render()).find(node => node.props?.['aria-label'] === '导入模型资格 JSON').props.onChange({ target: { files: [file], value: 'synthetic.json' } }) },
    async flush() { for (let i = 0; i < 8; i++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { disposed = true; for (const slot of slots) slot?.cleanup?.() },
    lateUpdates() { return lateUpdates },
  }
  globalThis.localAiTest = h
  globalThis.window = { setInterval: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id }, clearInterval: id => timers.delete(id), dispatchEvent() {} }
  h.render()
  return h
}

{
  const h = harness(url => { if (url === '/api/local-models/service') throw new Error('synthetic raw secret must not render') })
  await h.flush()
  assert.match(h.text(), /faster_whisper/)
  assert.match(h.text(), /Ollama 服务/)
  assert.doesNotMatch(h.text(), /raw secret/)
  assert.match(h.text(), /可用内存 0 B/)
  assert.equal(h.calls.some(call => call.method !== 'GET'), false, 'mounting diagnostics cannot load, download, record or import')
  h.dispose()
}
{
  const h = harness(url => { if (url === '/api/stt/settings' || url === '/api/local-models/resources') throw new Error('synthetic failure') })
  await h.flush()
  assert.match(h.text(), /STT Provider 状态未知/)
  assert.match(h.text(), /设备未确认/)
  assert.match(h.text(), /可用内存 未知/)
  assert.doesNotMatch(h.text(), /CPU（默认、稳定）/)
  h.dispose()
}
{
  const old = deferred(); let generation = 0
  const h = harness(url => {
    if (url === '/api/local-models/service') { generation++; return generation === 1 ? old.promise : { status: 'new-service' } }
    if (url === '/api/local-models/qualification') return qualification('NOT_RUN', generation === 1 ? 'old-model' : 'new-model')
  })
  h.button('刷新').props.onClick(); await h.flush()
  assert.match(h.text(), /new-model/)
  old.resolve({ status: 'old-service' }); await h.flush()
  assert.doesNotMatch(h.text(), /old-model|old-service/)
  h.dispose()
}
{
  const response = deferred()
  const h = harness(url => url === '/api/local-models/qualification' ? response.promise : undefined)
  h.dispose(); response.resolve(qualification('PASS')); await h.flush()
  assert.equal(h.lateUpdates(), 0, 'late diagnostics must not update an unmounted component')
}
{
  let stale = false
  const h = harness(url => url === '/api/local-models/qualification' ? qualification(stale ? 'STALE' : 'PASS') : undefined)
  await h.flush(); assert.match(h.text(), /已通过当前模型验收/)
  stale = true
  const timer = Array.from(h.timers.values()).find(timer => timer.delay === 30000)
  timer.callback(); await h.flush()
  assert.match(h.text(), /已失效，需重新验收/)
  assert.doesNotMatch(h.text(), /已通过当前模型验收/)
  assert.equal(h.calls.some(call => call.url.startsWith('/api/provider/health')), false, 'periodic qualification reads cannot probe or load a model')
  h.dispose()
}
{
  const h = harness(); await h.flush()
  h.upload({ size: 512 * 1024 + 1, text: async () => '{}' }); await h.flush()
  assert.match(h.text(), /不能超过 512 KiB/)
  assert.equal(h.calls.filter(call => call.url.endsWith('/qualification/import')).length, 0)
  h.upload({ size: 1, text: async () => '汉'.repeat(200000) }); await h.flush()
  assert.equal(h.calls.filter(call => call.url.endsWith('/qualification/import')).length, 0, 'UTF-8 byte bound also applies to an underreported synthetic File size')
  const pending = deferred()
  const file = { size: 2, text: () => pending.promise }
  h.upload(file); h.upload(file); pending.resolve('{}'); await h.flush()
  const imports = h.calls.filter(call => call.url.endsWith('/qualification/import'))
  assert.equal(imports.length, 1, 'same-render double import must have one owner')
  assert.deepEqual(imports[0].body, { report_json: '{}' })
  h.dispose()
}
{
  const h = harness(url => url === '/api/provider/configuration' ? { provider_id: 'deepseek', model: 'synthetic-cloud' } : undefined)
  await h.flush()
  assert.equal(h.button('刷新本地模型观测').props.disabled, true)
  h.button('刷新本地模型观测').props.onClick(); await h.flush()
  assert.equal(h.calls.some(call => call.url.startsWith('/api/provider/health')), false, 'cloud provider never receives a local-observation probe')
  h.dispose()
}
{
  const pending = deferred(); let changed = false
  const h = harness((url, _body, options) => {
    if (url === '/api/local-models/models') return [{ model_id: 'new-model', display_name: 'new-model', parameter_size: 'synthetic', quantization: 'synthetic', size: 0, size_vram: 0, loaded: false, context_length: 1024 }]
    if (url === '/api/provider/configuration' && options?.method === 'PUT') { changed = true; return pending.promise }
    if (url === '/api/local-models/qualification') return qualification(changed ? 'STALE' : 'PASS', changed ? 'new-model' : 'synthetic-model')
  })
  await h.flush(); assert.match(h.text(), /已通过当前模型验收/)
  h.button('设为对话模型').props.onClick()
  assert.doesNotMatch(h.text(), /已通过当前模型验收/, 'changing the model immediately withdraws the old qualification')
  pending.resolve({ provider_id: 'ollama', model: 'new-model', max_tokens: 32 }); await h.flush()
  assert.match(h.text(), /已失效，需重新验收/)
  assert.equal(h.calls.find(call => call.method === 'PUT').body.max_tokens, 32, 'selecting a local model preserves a small explicit budget')
  h.dispose()
}
console.log('LocalAiPanel: 8 production component diagnostic/qualification scenarios passed')
