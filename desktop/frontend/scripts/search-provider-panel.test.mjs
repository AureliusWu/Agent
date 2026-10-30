import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useCallback', 'useEffect', 'useRef', 'useState'].map(name => `export const ${name}=(...args)=>globalThis.searchPanelTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: 'data:text/javascript,export const Globe2="Globe2",ShieldCheck="ShieldCheck",Trash2="Trash2";' }
    if (specifier === '../../api') return { shortCircuit: true, url: 'data:text/javascript,export const api=(...args)=>globalThis.searchPanelTest.api(...args);' }
    if (specifier === '../../secrets') return { shortCircuit: true, url: 'data:text/javascript,export const saveDesktopSecret=(...args)=>globalThis.searchPanelTest.save(...args);export const deleteDesktopSecret=(...args)=>globalThis.searchPanelTest.remove(...args);export const hasDesktopSecret=(...args)=>globalThis.searchPanelTest.has(...args);' }
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
const { SearchProviderPanel } = await import('../src/components/providers/SearchProviderPanel.tsx')
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }

function harness(options = {}) {
  const slots = [], effects = [], requests = [], writes = [], deletions = []
  const saved = new Map(options.initialSaved || [])
  let cursor = 0, dirty = true, disposed = false, tree
  const h = {
    requests, writes, deletions, saved,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, value => { assert.equal(disposed, false, 'no state update after unmount'); slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; dirty = true }] },
      useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
      useCallback(callback, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { deps, callback }; return slots[index].callback },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) { const previous = slots[index]; slots[index] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = effect() }) } },
    },
    async api(path, request) {
      requests.push({ path, method: request?.method || 'GET' })
      if (path.endsWith('/health')) {
        if (options.healthGate) await options.healthGate.promise
        if (options.healthError) throw Object.assign(new Error('synthetic transport error; never expose supplied-key'), { status: 405 })
        return options.healthResult || { status: 'ok', latency_ms: 4 }
      }
      if (options.loadError) throw new Error('synthetic config failure')
      if (options.loadAfterWriteError && writes.length) throw new Error('synthetic refresh failure')
      if (options.loadAfterDeleteError && deletions.length) throw new Error('synthetic refresh failure')
      return { providers: ['tavily', 'brave'].map(name => ({ name, configured: saved.has(`${name}_api_key`), default: name === 'tavily' })) }
    },
    async save(name, value) { writes.push({ name, value }); if (options.saveGate) await options.saveGate.promise; if (options.saveError) throw new Error('supplied-key synthetic native failure'); saved.set(name, value) },
    async remove(name) { deletions.push(name); if (options.removeError) throw new Error('supplied-key synthetic native delete failure'); saved.delete(name) },
    async has(name) { return saved.has(name) },
    render() { globalThis.searchPanelTest = h; if (dirty && !disposed) { dirty = false; cursor = 0; tree = SearchProviderPanel(); for (const effect of effects.splice(0)) effect() } return tree },
    text() { return words(h.render()) },
    find(predicate) { return nodes(h.render()).find(predicate) },
    row(name) { return nodes(h.render()).find(node => node.props?.className === 'search-provider-row' && words(node).startsWith(name === 'tavily' ? 'Tavily' : 'Brave Search')) },
    input(name, value) { nodes(h.row(name)).find(node => node.type === 'input').props.onChange({ target: { value } }); h.render() },
    button(name, action = 'save') { return nodes(h.row(name)).find(node => node.type === 'button' && (action === 'save' ? words(node) === '保存并检查' || words(node) === '处理中…' : node.props['aria-label'] === `删除 ${name} 密钥`)) },
    async click(name, action) { const button = h.button(name, action); assert.ok(button && !button.props.disabled); button.props.onClick(); await h.flush() },
    async flush() { for (let index = 0; index < 8; index++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { for (const slot of slots) slot?.cleanup?.(); disposed = true },
  }
  h.render(); return h
}

{
  const h = harness(); await h.flush()
  assert.equal(h.requests.some(request => request.path.endsWith('/health')), false, 'opening search settings never probes cloud services')
  h.input('tavily', ' synthetic-key '); await h.click('tavily')
  assert.deepEqual(h.writes, [{ name: 'tavily_api_key', value: 'synthetic-key' }])
  assert.equal(h.requests.find(request => request.path.endsWith('/health')).method, 'POST', 'health endpoint only accepts POST')
  assert.match(h.text(), /密钥已保存.*连接正常/)
  assert.equal(nodes(h.row('tavily')).find(node => node.type === 'input').props.value, '')
  h.dispose()
}
{
  const h = harness({ healthError: true }); await h.flush()
  h.input('tavily', 'synthetic-key'); await h.click('tavily')
  assert.equal(h.saved.get('tavily_api_key'), 'synthetic-key')
  assert.match(h.text(), /密钥已保存.*检查失败/)
  assert.doesNotMatch(h.text(), /supplied-key|密钥未能保存/)
  assert.equal(h.button('tavily').props.disabled, true, 'cleared key does not silently resubmit')
  h.dispose()
}
{
  const h = harness({ healthResult: { status: 'error', error: 'supplied-key upstream echo' } }); await h.flush()
  h.input('brave', 'synthetic-key'); await h.click('brave')
  assert.match(h.text(), /密钥已保存.*检查未通过/)
  assert.doesNotMatch(h.text(), /supplied-key/)
  h.dispose()
}
{
  const h = harness({ saveError: true, initialSaved: [['tavily_api_key', 'original']] }); await h.flush()
  h.input('tavily', 'replacement'); await h.click('tavily')
  assert.equal(h.saved.get('tavily_api_key'), 'original')
  assert.match(h.text(), /密钥未能保存/)
  assert.doesNotMatch(h.text(), /supplied-key/)
  assert.equal(h.requests.some(request => request.path.endsWith('/health')), false)
  assert.equal(nodes(h.row('tavily')).find(node => node.type === 'input').props.value, 'replacement')
  h.dispose()
}
{
  const h = harness({ removeError: true, initialSaved: [['tavily_api_key', 'original']] }); await h.flush()
  await h.click('tavily', 'remove')
  assert.equal(h.saved.get('tavily_api_key'), 'original')
  assert.match(h.text(), /删除失败/)
  assert.doesNotMatch(h.text(), /supplied-key|密钥已删除/)
  assert.ok(h.button('tavily', 'remove'))
  h.dispose()
}
{
  const h = harness({ initialSaved: [['brave_api_key', 'original']] }); await h.flush()
  await h.click('brave', 'remove')
  assert.equal(h.saved.has('brave_api_key'), false)
  assert.match(h.text(), /密钥已删除/)
  assert.equal(h.button('brave', 'remove'), undefined)
  h.dispose()
}
{
  const gate = deferred(), h = harness({ saveGate: gate }); await h.flush()
  h.input('tavily', 'synthetic-key')
  const originalButton = h.button('tavily')
  originalButton.props.onClick(); originalButton.props.onClick(); await h.flush()
  assert.equal(h.writes.length, 1, 'same-tick repeated clicks cannot duplicate a native write')
  assert.equal(h.button('tavily').props.disabled, true)
  gate.resolve(); await h.flush(); assert.match(h.text(), /密钥已保存/)
  h.dispose()
}
{
  const h = harness({ loadAfterWriteError: true }); await h.flush()
  h.input('tavily', 'synthetic-key'); await h.click('tavily')
  assert.equal(h.saved.get('tavily_api_key'), 'synthetic-key')
  assert.match(h.text(), /密钥已保存.*状态刷新失败/)
  h.dispose()
}
{
  const h = harness({ loadAfterDeleteError: true, initialSaved: [['brave_api_key', 'original']] }); await h.flush()
  await h.click('brave', 'remove')
  assert.equal(h.saved.has('brave_api_key'), false)
  assert.match(h.text(), /密钥已删除.*状态刷新失败/)
  assert.equal(h.button('brave', 'remove'), undefined)
  h.dispose()
}
{
  const h = harness({ loadError: true }); await h.flush()
  assert.match(h.text(), /搜索配置读取失败/)
  assert.doesNotMatch(h.text(), /synthetic config failure/)
  h.dispose()
}
{
  const gate = deferred(), h = harness({ healthGate: gate }); await h.flush()
  h.input('tavily', 'synthetic-key'); h.button('tavily').props.onClick(); await h.flush()
  h.dispose(); gate.resolve(); await h.flush()
}
console.log('Search settings: POST health, native save/delete failures, saved-but-check-failed truth, duplicate-click guard, refresh errors and unmount fences passed')
