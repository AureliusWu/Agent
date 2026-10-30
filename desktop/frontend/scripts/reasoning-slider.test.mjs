import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Exercise production components and chat serialization with synthetic services.
// No browser, native credential access, network or model is available here.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect', 'useMemo', 'useId'].map(name => `export const ${name}=(...args)=>globalThis.reasoningTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['ArrowUp', 'ChevronDown', 'Gauge', 'LockKeyhole', 'Plus', 'RotateCcw', 'Send', 'Sparkles', 'Square', 'X', 'Zap'].map(name => `export const ${name}="${name}";`).join(''))}` }
    if (specifier === '../api') return { shortCircuit: true, url: 'data:text/javascript,export const api=(...args)=>globalThis.reasoningTest.api(...args);export class ApiError extends Error{};export const streamTaskEvents=()=>{throw new Error("unexpected stream")};' }
    if (specifier.endsWith('/commandRegistry')) return { shortCircuit: true, url: 'data:text/javascript,export const loadCommandCatalog=async()=>[];export const executeLocalCommand=async()=>false;' }
    if (specifier === './useTtsPlayback') return { shortCircuit: true, url: 'data:text/javascript,export const useTtsPlayback=()=>({interrupt:async()=>{}});' }
    if (specifier === './VoiceChatContext') return { shortCircuit: true, url: 'data:text/javascript,export const useVoiceChat=()=>null;' }
    if (['./AttachmentMenu', './VoiceInputControl', '../settings/PermissionMenu'].includes(specifier)) {
      const name = specifier.split('/').at(-1)
      return { shortCircuit: true, url: `data:text/javascript,export const ${name}=${JSON.stringify(name)};` }
    }
    if (specifier === '@tauri-apps/api/core') return { shortCircuit: true, url: 'data:text/javascript,export const invoke=()=>{throw new Error("native access forbidden")};' }
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

globalThis.fetch = () => { throw new Error('network forbidden') }
globalThis.document = { addEventListener() {}, removeEventListener() {} }
const saved = new Map()
globalThis.localStorage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) }
const { ModelReasoningMenu } = await import('../src/components/tasks/ModelReasoningMenu.tsx')
const { Composer } = await import('../src/components/chat/Composer.tsx')
const { useAgentChat } = await import('../src/hooks/useAgentChat.ts')
const { REASONING_STEPS, reasoningFromSliderIndex, reasoningSliderKeyValue } = await import('../src/shared/reasoningSlider.ts')
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
const noop = () => {}
function harness(component, initialProps, runEffects = true) {
  const slots = [], effects = [], requests = []
  let cursor = 0, dirty = true, tree, props = initialProps
  const h = {
    requests,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, update => { const next = typeof update === 'function' ? update(slots[index].value) : update; if (!Object.is(slots[index].value, next)) { slots[index].value = next; dirty = true } }] },
      useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
      useId() { return `reasoning-id-${cursor++}` },
      useMemo(calculate) { return calculate() },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) { slots[index]?.cleanup?.(); slots[index] = { deps }; if (runEffects) effects.push(() => { slots[index].cleanup = effect() }) } },
    },
    async api(url, options) { requests.push({ url, body: options?.body ? JSON.parse(options.body) : null }); throw new Error('synthetic capture only; task not executed') },
    render() { globalThis.reasoningTest = h; for (let count = 0; dirty && count < 10; count++) { dirty = false; cursor = 0; tree = component(props); for (const effect of effects.splice(0)) effect() } assert.equal(dirty, false, 'hooks settle'); return tree },
    update(next) { props = { ...props, ...next }; dirty = true; return h.render() },
    find(predicate) { return nodes(h.render()).find(predicate) },
    text() { return words(h.render()) },
  }
  h.render()
  return h
}
function menu(overrides = {}) {
  let h
  const changes = [], models = []
  h = harness(ModelReasoningMenu, { preferredModel: '', defaultModel: 'deepseek-flash', modelOptions: ['deepseek-flash', 'deepseek-v4-pro'], reasoningProvider: { id: 'deepseek' }, reasoningEffort: 'auto', onPreferredModel: model => { models.push(model); h.update({ preferredModel: model }) }, onReasoningEffort: effort => { changes.push(effort); h.update({ reasoningEffort: effort }) }, ...overrides })
  return { h, changes, models, range: () => h.find(node => node.type === 'input' && node.props.type === 'range'), reset: () => h.find(node => node.props?.['aria-label'] === '重置为自动推理') }
}
assert.deepEqual(REASONING_STEPS, ['auto', 'low', 'medium', 'high'])
for (const invalid of ['', '-1', '4', '1.5', 'NaN', 'Infinity']) assert.equal(reasoningFromSliderIndex(invalid), null)
assert.equal(reasoningSliderKeyValue('Enter', 'low'), null)
{
  const { h, changes, range, reset, models } = menu()
  assert.deepEqual([range().props.min, range().props.max, range().props.step], [0, 3, 1])
  assert.equal(range().props['aria-label'], '推理强度')
  assert.equal(range().props['aria-valuetext'], '自动推理')
  assert.ok(h.find(node => node.props?.id === range().props['aria-describedby']))
  assert.equal(reset().props.disabled, true)
  for (const [index, effort] of REASONING_STEPS.entries()) {
    range().props.onChange({ target: { value: String(index) } })
    assert.equal(changes.at(-1), effort, 'slider emits the real chat API value, not an index or visual label')
    assert.equal(range().props.value, index)
  }
  assert.equal(range().props['aria-valuetext'], '高推理')
  let prevented = 0
  for (const [key, expected] of [['Home', 'auto'], ['End', 'high'], ['ArrowLeft', 'medium'], ['ArrowDown', 'low'], ['ArrowRight', 'medium'], ['ArrowUp', 'high']]) {
    range().props.onKeyDown({ key, preventDefault() { prevented++ } })
    assert.equal(changes.at(-1), expected)
  }
  assert.equal(prevented, 6)
  const count = changes.length
  range().props.onKeyDown({ key: 'Home', ctrlKey: true, preventDefault() { throw new Error('modified shortcut must be left to host') } })
  range().props.onChange({ target: { value: '10' } })
  assert.equal(changes.length, count)
  reset().props.onClick()
  assert.equal(changes.at(-1), 'auto')
  assert.equal(reset().props.disabled, true)
  h.find(node => node.type === 'select').props.onChange({ target: { value: 'deepseek-v4-pro' } })
  assert.deepEqual(models, ['deepseek-v4-pro'])
  assert.match(h.text(), /deepseek-v4-pro/)
  h.update({ reasoningEffort: 'medium' })
  assert.match(h.text(), /映射为 High/)
  h.update({ reasoningProvider: { id: 'openai_compatible', capabilities: { reasoning: null } }, defaultModel: 'custom-local-model', preferredModel: '' })
  assert.equal(range().props.disabled, false, 'unknown capability does not disable existing compatible choices')
  assert.match(h.text(), /支持情况未确认/)
  assert.doesNotMatch(h.text(), /Ultra|Max|映射为 High/)
  h.update({ reasoningProvider: { id: 'mock', capabilities: { reasoning: false } } })
  assert.equal(range().props.disabled, true)
  assert.equal(range().props['aria-valuetext'], '自动推理')
  assert.match(h.text(), /当前模型不应用/)
  const beforeDisabled = changes.length
  range().props.onChange({ target: { value: '3' } })
  range().props.onKeyDown({ key: 'End', preventDefault() { throw new Error('disabled') } })
  reset().props.onClick()
  assert.equal(changes.length, beforeDisabled, 'unsupported UI cannot alter the saved preference')
  h.update({ reasoningProvider: { id: 'deepseek' } })
  assert.equal(range().props.value, 2, 'provider changes preserve the existing supported preference')
  h.update({ busy: true })
  assert.equal(range().props.disabled, true)
  assert.equal(h.find(node => node.type === 'select').props.disabled, true)
  const modelCount = models.length
  h.find(node => node.type === 'select').props.onChange({ target: { value: 'other' } })
  range().props.onChange({ target: { value: '0' } })
  assert.equal(models.length, modelCount)
  assert.equal(changes.length, beforeDisabled)
  assert.ok(nodes(h.render()).filter(node => node.type === 'button').every(node => node.props.type === 'button'))
  assert.match(h.text(), /标准速度.*当前 Provider 未提供速度切换/)
}
{
  const h = harness(Composer, { input: '', busy: false, stopState: 'idle', error: '', usage: null, context: null, mode: 'ask', reasoningEffort: 'high', preferredModel: '', defaultModel: 'deepseek-flash', modelOptions: ['deepseek-flash'], hasConversation: true, queuedItems: [], commands: [], onInput: noop, onMode: noop, onReasoningEffort: noop, onPreferredModel: noop, onSend: noop, onSteer: noop, onPromoteQueued: noop, onCancelQueued: noop, onStop: noop, onNavigate: noop, onUploadFile: noop, onClearError: noop })
  const trigger = () => h.find(node => node.props?.['aria-label'] === '切换模型与推理')
  trigger().props.onClick()
  assert.equal(trigger().props['aria-expanded'], true)
  assert.ok(h.find(node => node.type === ModelReasoningMenu))
  h.update({ busy: true })
  assert.equal(trigger().props.disabled, true)
  assert.equal(trigger().props['aria-expanded'], false)
  assert.equal(h.find(node => node.type === ModelReasoningMenu), undefined)
  trigger().props.onClick()
  h.update({ busy: false })
  assert.equal(trigger().props['aria-expanded'], false, 'a busy transition closes rather than reopens stale menus')
  h.update({ reasoningProvider: { id: 'mock', capabilities: { reasoning: false } } })
  assert.match(words(trigger()), /自动推理/)
  assert.doesNotMatch(words(trigger()), /高推理/)
}
{
  // Integrate the actual menu callbacks with the actual hook's POST body.
  // The synthetic API captures then fails before a task can run.
  saved.clear()
  const chat = harness(() => useAgentChat({ id: 17, workspace: 'synthetic' }, noop, noop), {}, false)
  for (const [index, expected] of REASONING_STEPS.entries()) {
    const current = chat.render()
    const { h: menuHarness, range } = menu({ reasoningEffort: current.reasoningEffort, onReasoningEffort: current.setReasoningEffort, onPreferredModel: current.setPreferredModel })
    const selectedModel = index % 2 ? 'deepseek-v4-pro' : 'deepseek-flash'
    menuHarness.find(node => node.type === 'select').props.onChange({ target: { value: selectedModel } })
    range().props.onChange({ target: { value: String(index) } })
    const updated = chat.render()
    assert.equal(updated.reasoningEffort, expected)
    await updated.send('synthetic serialization probe')
    assert.equal(chat.requests.at(-1).url, '/api/tasks')
    assert.equal(chat.requests.at(-1).body.reasoning_effort, expected, 'POST body retains the exact slider value')
    assert.equal(chat.requests.at(-1).body.preferred_model, selectedModel, 'slider changes do not overwrite the chosen model')
    assert.equal(saved.get('agent_reasoning_effort'), expected)
  }
}
console.log('Reasoning slider: discrete real API values, keyboard/Home/End, reset, accessibility, model/provider changes, busy close and synthetic task serialization passed')
