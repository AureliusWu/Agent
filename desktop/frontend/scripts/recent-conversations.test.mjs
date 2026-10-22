import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Execute the production component with synthetic DOM geometry and React hooks.
// This proves portal placement and behavior, not browser layout or pixel visibility.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect', 'useLayoutEffect'].map(name => `export const ${name}=(...args)=>globalThis.recentTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'react-dom') return { shortCircuit: true, url: 'data:text/javascript,export const createPortal=(children,container)=>({type:"test-portal",props:{children},container});' }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['MessageSquare', 'MoreHorizontal', 'Pencil', 'Trash2'].map(name => `export const ${name}="${name}";`).join(''))}` }
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

const { RecentConversationList } = await import('../src/components/chat/RecentConversationList.tsx')
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const sameDeps = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]))
const item = id => ({ id, title: `synthetic-${id}`, updated_at: '2026-10-22T08:00:00Z' })

function eventSurface() {
  const listeners = new Map()
  return {
    listeners,
    addEventListener(type, callback, capture = false) { const entries = listeners.get(type) || []; entries.push({ callback, capture }); listeners.set(type, entries) },
    removeEventListener(type, callback, capture = false) { listeners.set(type, (listeners.get(type) || []).filter(entry => entry.callback !== callback || entry.capture !== capture)) },
    emit(type, fields = {}) { const event = { preventDefault() { this.defaultPrevented = true }, stopPropagation() { this.propagationStopped = true }, ...fields }; for (const entry of [...listeners.get(type) || []]) entry.callback(event); return event },
    listenerCount() { return [...listeners.values()].reduce((total, entries) => total + entries.length, 0) },
  }
}

function harness(options = {}) {
  const document = eventSurface(), window = { ...eventSurface(), innerWidth: 800, innerHeight: 600, ...options.viewport }
  class SyntheticNode {
    constructor(type = 'div', props = {}) { this.type = type; this.props = props; this.children = []; this.parentNode = null; this.isConnected = true; this.rect = { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 } }
    contains(target) { for (let current = target; current; current = current.parentNode) if (current === this) return true; return false }
    focus() { document.activeElement = this; document.emit('focusin', { target: this }) }
    getBoundingClientRect() { return this.rect }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null }
    querySelectorAll(selector) { return this.children.flatMap(child => [...(selector === 'button' && child.type === 'button' ? [child] : []), ...child.querySelectorAll(selector)]) }
  }
  document.body = new SyntheticNode('body')
  const cache = new Map(), elementMap = new WeakMap(), slots = [], pendingLayout = [], pendingEffects = [], calls = []
  let cursor = 0, dirty = true, disposed = false, tree
  const props = { conversations: [item(1), item(2)], active: item(1), onSelect(value) { calls.push(['select', value.id]) }, onRename(value) { calls.push(['rename', value.id]) }, onDelete(value) { calls.push(['delete', value.id]) }, ...options.props }
  function effectHook(queue, effect, deps) {
    const index = cursor++, previous = slots[index]
    if (!previous || !sameDeps(previous.deps, deps)) { slots[index] = { deps, cleanup: previous?.cleanup }; queue.push(() => { slots[index].cleanup?.(); slots[index].cleanup = effect() }) }
  }
  function commit() {
    for (const element of cache.values()) { element.isConnected = false; element.children = [] }
    for (const slot of slots) if (slot && 'current' in slot) slot.current = null
    document.body.children = []
    function mount(value, parent, path = 'root') {
      if (Array.isArray(value)) { value.forEach((child, index) => mount(child, parent, `${path}.${index}`)); return }
      if (!value || typeof value !== 'object') return
      if (value.type === 'test-portal') { mount(value.props.children, value.container, `${path}.portal`); return }
      const key = value.props?.id || `${path}:${value.type}`
      let element = cache.get(key)
      if (!element) { element = new SyntheticNode(value.type, value.props); cache.set(key, element) }
      element.props = value.props; element.isConnected = true; element.parentNode = parent; parent.children.push(element); elementMap.set(value, element)
      if (value.props?.className === 'recent-menu') element.rect = { left: 0, right: 116, top: 0, bottom: 74, width: 116, height: 74 }
      if (value.props?.ref) value.props.ref.current = element
      mount(value.props?.children, element, `${path}.children`)
    }
    mount(tree, document.body)
  }
  const h = {
    props, calls, window, document,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, value => { if (disposed) return; const next = typeof value === 'function' ? value(slots[index].value) : value; if (!Object.is(next, slots[index].value)) { slots[index].value = next; dirty = true } }] },
      useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
      useEffect(effect, deps) { effectHook(pendingEffects, effect, deps) },
      useLayoutEffect(effect, deps) { effectHook(pendingLayout, effect, deps) },
    },
    render() {
      globalThis.recentTest = h; globalThis.document = document; globalThis.window = window; globalThis.Node = SyntheticNode
      let passes = 0
      while (dirty && !disposed) { assert.ok(++passes < 20, 'render settles'); dirty = false; cursor = 0; tree = RecentConversationList(props); commit(); for (const effect of pendingLayout.splice(0)) effect(); for (const effect of pendingEffects.splice(0)) effect() }
      return tree
    },
    find(predicate) { const node = nodes(h.render()).find(predicate); assert.ok(node, 'expected production element exists'); return node },
    trigger(id) { return h.find(node => ['button', 'summary'].includes(node.type) && node.props?.['aria-label'] === `synthetic-${id}的更多操作`) },
    element(node) { return elementMap.get(node) },
    open(id, rect = { left: 180, right: 208, top: 100, bottom: 130, width: 28, height: 30 }) { const trigger = h.trigger(id), element = h.element(trigger); element.rect = rect; assert.equal(trigger.type, 'button', 'more control remains natively Enter/Space accessible'); assert.equal(trigger.props.type, 'button'); trigger.props.onClick({ currentTarget: element }); h.render() },
    menu() { return h.find(node => node.props?.className === 'recent-menu') },
    action(label) { return h.find(node => node.type === 'button' && words(node) === label) },
    hasMenu() { return nodes(h.render()).some(node => node.props?.className === 'recent-menu') },
    update(next) { Object.assign(props, next); dirty = true; h.render() },
    outside(focus = true) { const node = new SyntheticNode('button'); node.parentNode = document.body; if (focus) node.focus(); return node },
    dispose() { disposed = true; for (const slot of slots) slot?.cleanup?.(); for (const element of cache.values()) element.isConnected = false },
  }
  h.render()
  return h
}

// The previous implementation rendered an absolute menu inside overflow:auto.
// A portal must be absent when closed and attached to body (not the recent list) when open.
{
  const h = harness()
  assert.equal(h.hasMenu(), false, 'closed menus must not remain in the clipping list DOM')
  h.open(1)
  const portal = h.find(node => node.type === 'test-portal')
  assert.equal(portal.container, h.document.body)
  const menu = h.element(h.menu()), recentList = h.element(h.find(node => node.props?.className === 'recent-list'))
  assert.equal(recentList.contains(menu), false)
  assert.equal(menu.parentNode, h.document.body)
  assert.equal(h.trigger(1).props['aria-expanded'], true)
  assert.equal(h.menu().props.role, 'group', 'ordinary buttons do not claim unimplemented menu keyboard semantics')
  assert.equal(h.document.activeElement, h.element(h.action('重命名')))
  assert.equal(h.menu().props.style.position, 'fixed')
  assert.equal(h.menu().props.style.left, 92)
  assert.equal(h.menu().props.style.top, 133)
  h.dispose()
}

// Geometry is synthetic: verify the production flip/clamp calculation, not pixels.
for (const { rect, viewport, expected } of [
  { rect: { left: 180, right: 208, top: 550, bottom: 580, width: 28, height: 30 }, expected: { left: 92, top: 473 } },
  { rect: { left: 1, right: 29, top: 1, bottom: 31, width: 28, height: 30 }, expected: { left: 8, top: 34 } },
  { rect: { left: 790, right: 818, top: 100, bottom: 130, width: 28, height: 30 }, expected: { left: 676, top: 133 } },
  { rect: { left: 80, right: 108, top: 32, bottom: 62, width: 28, height: 30 }, viewport: { innerWidth: 160, innerHeight: 80 }, expected: { left: 8, top: 8 } },
]) {
  const h = harness({ viewport }); h.open(1, rect)
  const { left, top } = h.menu().props.style
  assert.deepEqual({ left, top }, expected)
  assert.equal(h.menu().props.style.maxHeight, h.window.innerHeight - 16)
  h.dispose()
}

{
  const h = harness(); h.open(1); h.open(2)
  assert.equal(nodes(h.render()).filter(node => node.type === 'test-portal').length, 1)
  assert.equal(h.trigger(1).props['aria-expanded'], false)
  assert.equal(h.trigger(2).props['aria-expanded'], true)
  h.document.emit('pointerdown', { target: h.element(h.action('重命名')) }); h.render()
  assert.equal(h.hasMenu(), true, 'a portal-internal pointerdown does not dismiss before the action')
  h.action('重命名').props.onClick(); h.render()
  assert.deepEqual(h.calls, [['rename', 2]], 'rename is routed once to the correct item, never selection/deletion')
  assert.equal(h.hasMenu(), false)
  h.open(1); h.action('删除').props.onClick(); h.render()
  assert.deepEqual(h.calls, [['rename', 2], ['delete', 1]])
  h.open(1); h.open(1)
  assert.equal(h.hasMenu(), false, 'the same trigger toggles its single popup closed')
  h.dispose()
}

{
  const h = harness(); h.open(1)
  const anchor = h.element(h.trigger(1))
  h.document.emit('pointerdown', { target: anchor }); h.render()
  assert.equal(h.hasMenu(), true, 'trigger pointerdown does not count as outside')
  const escape = h.document.emit('keydown', { key: 'Escape' }); h.render()
  assert.equal(escape.defaultPrevented, true)
  assert.equal(h.hasMenu(), false)
  assert.equal(h.document.activeElement, anchor, 'Escape restores the opening control')
  assert.deepEqual(h.calls, [])
  h.open(1); const external = h.outside(false); h.document.emit('pointerdown', { target: external }); h.render()
  assert.equal(h.hasMenu(), false)
  assert.notEqual(h.document.activeElement, anchor, 'pointer dismissal does not restore anchor focus')
  external.focus()
  assert.equal(h.document.activeElement, external, 'outside dismissal never steals focus')
  h.open(1); h.element(h.action('删除')).focus(); h.render()
  assert.equal(h.hasMenu(), true, 'Tab focus inside the ordinary button group remains usable')
  const nextFocus = h.outside(); h.render()
  assert.equal(h.hasMenu(), false, 'Tab focus leaving the group dismisses without a focus trap')
  assert.equal(h.document.activeElement, nextFocus)
  assert.deepEqual(h.calls, [])
  h.dispose()
}

for (const type of ['scroll', 'resize']) {
  const h = harness(); h.open(1)
  if (type === 'scroll') { h.window.emit(type, { target: h.element(h.menu()) }); h.render(); assert.equal(h.hasMenu(), true, 'the overflow-capable popup may scroll itself') }
  h.window.emit(type, { target: h.element(h.find(node => node.props?.className === 'recent-list')) }); h.render()
  assert.equal(h.hasMenu(), false, `${type} dismisses stale anchor coordinates`)
  assert.equal(h.document.listenerCount() + h.window.listenerCount(), 0)
  h.dispose()
}

{
  const h = harness(); h.open(1); h.update({ active: item(2) }); assert.equal(h.hasMenu(), false)
  h.open(1); h.update({ conversations: [item(2)] }); assert.equal(h.hasMenu(), false)
  assert.deepEqual(h.calls, [])
  h.open(2); h.dispose()
  assert.equal(h.document.listenerCount() + h.window.listenerCount(), 0, 'unmount removes every listener')
}

// A body portal needs its own stacking layer above the narrow desktop drawer,
// rather than inheriting that drawer's layer. Keep it below the main header.
const sidebarCss = readFileSync(new URL('../src/styles/sidebar.css', import.meta.url), 'utf8')
const responsiveCss = readFileSync(new URL('../src/styles/responsive.css', import.meta.url), 'utf8')
const layoutCss = readFileSync(new URL('../src/styles/layout.css', import.meta.url), 'utf8')
const menuLayer = Number(sidebarCss.match(/\.recent-menu\s*\{[^}]*z-index:\s*(\d+)/)?.[1])
const drawerLayer = Number(responsiveCss.match(/\.workbench-sidebar\s*\{[^}]*z-index:\s*(\d+)/)?.[1])
const headerLayer = Number(layoutCss.match(/\.top-header\s*\{[^}]*z-index:\s*(\d+)/)?.[1])
assert.ok(menuLayer > drawerLayer, 'the portal is above the narrow desktop drawer')
assert.ok(menuLayer < headerLayer, 'the portal does not override the main header')

// Exercise the unchanged production App callbacks with synthetic prompt/confirm/API.
// No App mounting, native credential command, user session, or network is involved.
const appSource = ts.createSourceFile('App.tsx', readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
function appHandler(name, scope) {
  const matches = []
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === name) matches.push(node); ts.forEachChild(node, visit) }
  visit(appSource); assert.equal(matches.length, 1)
  const compiled = ts.transpileModule(matches[0].getText(appSource), { compilerOptions: { target: ts.ScriptTarget.ES2023, module: ts.ModuleKind.ESNext } }).outputText
  return new Function(...Object.keys(scope), `${compiled};return ${name}`)(...Object.values(scope))
}
{
  const calls = [], value = item(99)
  const scope = { confirm: () => false, api: async (...args) => { calls.push(args) }, setConversations() { throw new Error('refused deletion must not mutate state') }, active: value, setActive() { throw new Error('refused deletion must retain active item') }, chat: { resetConversation() { throw new Error('refused deletion must not reset chat') }, setError() {} }, localStorage: { removeItem() { throw new Error('refused deletion must not clear active item') } } }
  await appHandler('removeConversation', scope)(value)
  assert.deepEqual(calls, [], 'original confirmation refusal must issue zero DELETE calls')
  const mutations = []
  await appHandler('removeConversation', { ...scope, confirm: () => true, active: null, setConversations: update => mutations.push(update([item(1), value])) })(value)
  assert.deepEqual(calls, [[`/api/conversations/${value.id}`, { method: 'DELETE' }]])
  assert.deepEqual(mutations.map(items => items.map(entry => entry.id)), [[1]])
  const renamed = []
  await appHandler('renameConversation', { prompt: () => ' renamed ', api: async (...args) => renamed.push(args), setConversations() {}, active: null, setActive() {}, chat: { setError() {} } })(value)
  assert.deepEqual(renamed, [[`/api/conversations/${value.id}`, { method: 'PATCH', body: JSON.stringify({ title: 'renamed' }) }]])
}

console.log('V16 recent conversations: production portal/geometry/dismissal/focus/callback and unchanged confirmation regression tests passed (synthetic DOM, not pixel acceptance)')
