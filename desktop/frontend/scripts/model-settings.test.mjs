import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Exercise production UI and save/request helpers with synthetic services only.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect'].map(name => `export const ${name}=(...args)=>globalThis.modelSettingsTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['ChevronDown', 'ExternalLink', 'KeyRound', 'Plus', 'RefreshCw'].map(name => `export const ${name}="${name}";`).join(''))}` }
    if (specifier === '../../api') return { shortCircuit: true, url: 'data:text/javascript,export const api=(...args)=>globalThis.modelSettingsTest.api(...args);' }
    if (specifier === '../../secrets') return { shortCircuit: true, url: 'data:text/javascript,export const saveDesktopSecret=(...args)=>globalThis.modelSettingsTest.saveKey(...args);' }
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.endsWith('.css')) return { shortCircuit: true, format: 'module', source: 'export default {}' }
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText }
    return nextLoad(url, context)
  },
})
const { DeepSeekProviderPanel } = await import('../src/components/providers/DeepSeekProviderPanel.tsx')
const { availableChatModels, assertCredentialDestination, modelConnectionMessage, modelDirectoryRequest, saveModelSettings } = await import('../src/shared/modelSettings.ts')
const { modelCredentialAlias, mayUseLegacyModelCredential } = await import('../src/shared/modelCredentials.ts')
const initial = { provider_id: 'deepseek', base_url: 'https://api.deepseek.com', model: 'deepseek-flash', timeout_seconds: 90, max_tokens: 8192, max_retries: 0, allow_tools: true, allow_streaming: true, configuration_revision: '1'.repeat(32) }
const applied = configuration => ({ ...configuration, configuration_revision: crypto.randomUUID().replaceAll('-', '') })
const policy = { provider: { id: 'deepseek', default_model: 'selected-custom-model', models: ['deepseek-flash', 'deepseek-v4-pro'] }, models: { light: 'deepseek-flash', medium: 'deepseek-flash', strong: 'deepseek-flash' } }
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
function harness(custom = () => undefined) {
  const slots = [], effects = [], calls = [], keyWrites = [], events = []
  let cursor = 0, dirty = true, tree, disposed = false, lateUpdates = 0
  const h = {
    calls, keyWrites, events,
    hooks: {
      useState(initialValue) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initialValue === 'function' ? initialValue() : initialValue }; return [slots[index].value, value => { if (disposed) lateUpdates++; slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; dirty = true }] },
      useRef(initialValue) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initialValue }; return slots[index] },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) { const previous = slots[index]; slots[index] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = effect() }) } },
    },
    async api(url, options) {
      const body = options?.body ? JSON.parse(options.body) : undefined
      calls.push({ url, method: options?.method || 'GET', body, headers: options?.headers })
      const special = custom(url, body, options)
      if (special !== undefined) return special
      if (url === '/api/provider/configuration') return body ? applied(body) : { ...initial }
      if (url === '/api/provider/policy') return policy
      if (url === '/api/local-models/models') return [{ model_id: 'installed-a', display_name: 'Installed A', installed: true }, { model_id: 'installed-b', display_name: 'Installed B', installed: true }]
      if (url === '/api/provider/models') return { status: 'ok', provider: 'synthetic', model: body.model, latency_ms: 1, models: [{ id: 'remote-model', name: 'Remote Model' }] }
      throw new Error(`Unexpected request ${url}`)
    },
    async saveKey(name, value) { keyWrites.push({ name, value }); if (custom('save-key') === false) throw new Error('synthetic-native-failure') },
    render() { globalThis.modelSettingsTest = h; if (dirty && !disposed) { dirty = false; cursor = 0; tree = DeepSeekProviderPanel(); for (const effect of effects.splice(0)) effect() } return tree },
    text() { return words(h.render()) },
    find(predicate) { return nodes(h.render()).find(predicate) },
    field(id, value) { const input = h.find(node => node.props?.id === id || node.props?.['aria-label'] === id); assert.ok(input, `field ${id}`); input.props.onChange({ target: { value } }); h.render() },
    async click(label) { const button = h.find(node => node.type === 'button' && words(node) === label); assert.ok(button, `button ${label}`); await button.props.onClick(); await h.flush() },
    async submit() { await h.find(node => node.type === 'form').props.onSubmit({ preventDefault() {} }); await h.flush() },
    async flush() { for (let index = 0; index < 6; index++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { disposed = true; for (const slot of slots) slot?.cleanup?.() },
    lateUpdates: () => lateUpdates,
  }
  globalThis.modelSettingsTest = h
  globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent(event) { events.push(event.type) } }
  h.render()
  return h
}

assert.deepEqual(availableChatModels(policy), ['selected-custom-model', 'deepseek-flash', 'deepseek-v4-pro'], 'chat menu retains configured model and provider catalog, not only routing tiers')
const draft = { ...initial, model: 'draft-model', timeout_seconds: 37 }
const request = modelDirectoryRequest(draft, ' synthetic-key ')
assert.equal(request.method, 'POST')
assert.deepEqual(JSON.parse(request.body), JSON.parse(JSON.stringify({ ...draft, configuration_revision: undefined })), 'connection checks use unsaved model values, not the CAS token')
assert.equal(request.headers['X-Model-Api-Key'], 'synthetic-key')
assert.equal(request.body.includes('synthetic-key'), false, 'credentials never enter persisted configuration JSON')
for (const local of [{ ...initial, provider_id: 'ollama' }, { ...initial, provider_id: 'mock' }, { ...initial, provider_id: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1' }]) {
  assert.deepEqual(modelDirectoryRequest(local, 'synthetic-cloud-key').headers, { 'X-Siyi-Omit-Model-Credential': '1' })
}
assert.throws(() => assertCredentialDestination({ ...initial, provider_id: 'openai_compatible', base_url: 'https://different.example/v1' }, initial, ''), /原有密钥不会自动发送/)
assert.doesNotThrow(() => assertCredentialDestination({ ...initial, base_url: 'https://api.deepseek.com/v1', model: 'deepseek-v4-pro' }, initial, ''))
assert.equal(await modelCredentialAlias(initial), await modelCredentialAlias({ ...initial, base_url: 'https://api.deepseek.com/v1' }))
assert.notEqual(await modelCredentialAlias(initial), await modelCredentialAlias({ ...initial, provider_id: 'openai_compatible', base_url: 'https://another.example/v1' }))
assert.equal(mayUseLegacyModelCredential(initial), true)
assert.equal(mayUseLegacyModelCredential({ ...initial, provider_id: 'openai_compatible' }), false)
assert.match(modelConnectionMessage({ status: 'error', provider: 'synthetic', model: '', latency_ms: 1, models: [], error_type: 'runtime_file_missing' }), /运行文件缺失/)
assert.match(modelConnectionMessage({ status: 'ok', provider: 'synthetic', model: '', latency_ms: 1, models: [] }), /未执行付费对话/)
{
  let saved = 0, keys = 0
  await saveModelSettings(initial, '', async (configuration, revision) => { saved++; assert.equal(revision, initial.configuration_revision); return applied(configuration) }, async () => { keys++ })
  assert.equal(saved, 1); assert.equal(keys, 0, 'blank API Key preserves existing secret without reading or deleting it')
  await assert.rejects(saveModelSettings({ ...initial, provider_id: 'openai_compatible', base_url: 'https://other.example/v1' }, '', async configuration => { saved++; return configuration }, async () => {}, initial), /提供商或 API 地址已改变/)
  assert.equal(saved, 1, 'unsafe credential destination change must stop before configuration write')
}
{
  const old = { ...initial, credential_generation: 'a'.repeat(32), configuration_revision: 'b'.repeat(32) }
  const activeAlias = await modelCredentialAlias(old)
  const secrets = new Map([[activeAlias, 'synthetic-old-key']])
  let disk = old
  let attempted
  const stage = async (name, value) => {
    assert.equal(secrets.has(name), false, 'candidate never overwrites any selected or previously staged slot')
    secrets.set(name, value)
  }
  // Same-endpoint key change fails before config commit.
  await assert.rejects(saveModelSettings(old, 'synthetic-new-key', async (config, revision) => {
    assert.equal(revision, old.configuration_revision)
    attempted = config
    assert.equal(secrets.get(await modelCredentialAlias(config)), 'synthetic-new-key')
    throw new Error('synthetic-sensitive-error')
  }, stage, old, async () => disk), error => /未能确认保存结果/.test(error.message) && !/synthetic/.test(error.message))
  assert.equal(disk, old)
  assert.equal(secrets.get(activeAlias), 'synthetic-old-key')
  const inactiveAlias = await modelCredentialAlias(attempted)
  assert.notEqual(inactiveAlias, activeAlias)
  // Simulate durable write + disconnected response + readback in a new
  // transport. Exactly one PUT and a read-only check; no retry or delete.
  let puts = 0, reads = 0
  const recovered = await saveModelSettings(old, 'synthetic-committed-key', async config => {
    puts++
    disk = applied(config)
    throw new Error('lost response')
  }, stage, old, async () => { reads++; return { ...disk } })
  assert.deepEqual(recovered, disk)
  assert.equal(puts, 1); assert.equal(reads, 1)
  assert.equal(secrets.get(await modelCredentialAlias(recovered)), 'synthetic-committed-key')
  assert.equal(secrets.get(activeAlias), 'synthetic-old-key')
  assert.equal(secrets.get(inactiveAlias), 'synthetic-new-key')
  // Delayed commit can still happen after a readback of the old revision.
  // Such a GET cannot prove failure or authorize deleting the candidate.
  let delayed
  await assert.rejects(saveModelSettings(disk, 'synthetic-delayed-key', async config => {
    delayed = config
    throw new Error('in flight')
  }, stage, disk, async () => disk), /未能确认保存结果/)
  disk = applied(delayed)
  assert.equal(secrets.get(await modelCredentialAlias(disk)), 'synthetic-delayed-key')
  // No secret draft uses the active generation, without touching the vault.
  const sameKey = await saveModelSettings({ ...disk, model: 'new-model' }, '', async config => applied(config), stage, disk)
  assert.equal(sameKey.credential_generation, disk.credential_generation)
  assert.equal(secrets.size, 4)
  await assert.rejects(saveModelSettings(disk, 'synthetic-rejected', async () => {
    throw Object.assign(new Error('synthetic-sensitive-detail'), { status: 409 })
  }, stage, disk), error => /保存被拒绝/.test(error.message) && !/synthetic/.test(error.message))
}
{
  const unsupported = { ...initial, configuration_revision: undefined }
  await assert.rejects(saveModelSettings(unsupported, 'synthetic-key', async () => assert.fail('must not PUT'), async () => assert.fail('must not stage')), /尚未确认支持安全保存/)
  const existing = { ...initial, credential_generation: 'a'.repeat(32) }
  const switched = { ...existing, provider_id: 'openai_compatible', base_url: 'https://different.example/v1' }
  assert.equal(JSON.parse(modelDirectoryRequest(switched, 'synthetic-explicit-key', existing).body).credential_generation, undefined)
  assert.equal(JSON.parse(modelDirectoryRequest(existing, '', existing).body).credential_generation, existing.credential_generation)
}
{
  const h = harness(); await h.flush()
  assert.equal(h.calls.some(call => call.url === '/api/provider/models' || call.url === '/api/provider/health'), false, 'opening settings never probes a cloud model')
  assert.equal(h.find(node => node.type === 'details').props.open, undefined, 'advanced fields default collapsed')
  assert.match(h.text(), /获取可用模型/)
  await h.submit()
  assert.equal(h.keyWrites.length, 0)
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 1)
  assert.equal(h.calls.some(call => call.url === '/api/provider/models' || call.url === '/api/provider/health'), false, 'save is independent from upstream validation')
  assert.ok(h.events.includes('siyi:provider-settings-changed'))
  h.dispose()
}
{
  const h = harness(); await h.flush()
  h.field('deepseek-api-key', 'synthetic-replacement')
  await h.submit()
  const save = h.calls.find(call => call.method === 'PUT')
  assert.match(save.body.credential_generation, /^[a-f0-9]{32}$/)
  assert.equal(save.headers['X-Siyi-Provider-Revision'], initial.configuration_revision)
  assert.deepEqual(h.keyWrites, [{ name: await modelCredentialAlias(save.body), value: 'synthetic-replacement' }])
  assert.notEqual(h.keyWrites[0].name, await modelCredentialAlias(initial))
  assert.equal(h.find(node => node.props?.id === 'deepseek-api-key').props.value, '')
  assert.equal(h.calls.some(call => call.url === '/api/provider/models'), false)
  h.dispose()
}
{
  const h = harness(); await h.flush()
  h.field('model-provider', 'openai_compatible')
  h.field('compatible-endpoint', 'https://new-provider.example/v1')
  await h.click('检查连接')
  assert.match(h.text(), /原有密钥不会自动发送/)
  assert.equal(h.calls.some(call => call.url === '/api/provider/models'), false)
  h.field('deepseek-api-key', 'synthetic-new-provider-key')
  await h.click('检查连接')
  const check = h.calls.find(call => call.url === '/api/provider/models')
  assert.equal(check.body.base_url, 'https://new-provider.example/v1')
  assert.equal(check.headers['X-Model-Api-Key'], 'synthetic-new-provider-key')
  assert.equal(h.calls.some(call => call.method === 'PUT'), false, 'checking draft never saves it')
  assert.match(h.text(), /未执行付费对话/)
  h.dispose()
}
{
  const h = harness(); await h.flush()
  await h.click('添加模型'); h.field('新模型 ID', 'custom-model-2'); await h.click('添加并选用')
  assert.equal(h.find(node => node.props?.id === 'provider-model').props.value, 'custom-model-2')
  await h.submit(); assert.equal(h.calls.find(call => call.method === 'PUT').body.model, 'custom-model-2')
  h.field('model-provider', 'ollama')
  assert.equal(h.find(node => node.props?.id === 'deepseek-api-key'), undefined)
  assert.match(h.text(), /Installed A/); assert.match(h.text(), /Installed B/)
  await h.click('检查连接')
  assert.deepEqual(h.calls.findLast(call => call.url === '/api/provider/models').headers, { 'X-Siyi-Omit-Model-Credential': '1' })
  h.dispose()
}
{
  const h = harness(url => url === 'save-key' ? false : undefined); await h.flush()
  h.field('deepseek-api-key', 'synthetic-key')
  await h.submit()
  assert.match(h.text(), /API Key 未能保存.*模型配置没有改变/)
  assert.equal(h.calls.some(call => call.method === 'PUT'), false, 'a native secret failure cannot switch configuration while retaining a mismatched old secret')
  assert.doesNotMatch(h.text(), /synthetic-native-failure/)
  h.dispose()
}
console.log('Model/API settings: offline save, draft discovery, credential destinations, local fences, model catalog and UI behavior passed')
await import('./model-credential-aliases.test.mjs')
