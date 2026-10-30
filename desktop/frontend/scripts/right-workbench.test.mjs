import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Real component and hook; only React scheduling and the broker boundary are
// simulated. These tests never launch a desktop app or read a real workspace.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx = (type,props,key) => ({type,props,key}); export const jsxs = jsx; export const Fragment = "Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect', 'useCallback'].map(name => `export const ${name} = (...args) => globalThis.rightWorkbenchTest.hooks.${name}(...args)`).join('\n'))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['Activity', 'ArrowUp', 'ChevronRight', 'FileText', 'Folder', 'FolderOpen', 'Home', 'PanelRightClose', 'RefreshCw', 'Waves', 'X'].map(name => `export const ${name} = '${name}'`).join('\n'))}` }
    if (specifier.endsWith('/api')) return { shortCircuit: true, url: 'data:text/javascript,export const api = (...args) => globalThis.rightWorkbenchTest.api(...args)' }
    if (specifier.endsWith('/KokoroPanel')) return { shortCircuit: true, url: 'data:text/javascript,export const KokoroPanel = "KokoroPanel"' }
    if (specifier.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export default {}' }
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
const { RightWorkbench } = await import('../src/components/workspace/RightWorkbench.tsx')
const { workspaceRelativePath, workspaceEntries, workbenchElementId, MAX_PREVIEW_TABS, MAX_PREVIEW_CHARS } = await import('../src/shared/rightWorkbench.ts')
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const sample = { name: 'note.txt', path: 'note.txt', type: 'file', size: 12 }
const props = { open: false, active: { id: 42, title: 'Synthetic conversation', workspace: 'synthetic-workspace' }, workspace: 'synthetic-workspace', mode: 'readonly', context: null, busy: false, pendingCount: 0, recoverable: false, messageCount: 2, runtimeEvents: [], verification: null, onClose() {}, onNavigate() {}, onCompact() {}, onChooseWorkspace() {} }

function harness(options = {}) {
  const slots = [], effects = [], calls = []
  let cursor = 0, dirty = true, disposed = false, tree
  const currentProps = { ...props, ...options.props }
  const component = RightWorkbench(currentProps).type
  const h = {
    calls,
    focused: null,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[index].value, value => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value; dirty = true }] },
      useRef(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { current: initial }; return slots[index] },
      useCallback(callback, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) slots[index] = { deps, callback }; return slots[index].callback },
      useEffect(effect, deps) { const index = cursor++; if (!slots[index] || !sameDeps(slots[index].deps, deps)) { const previous = slots[index]; slots[index] = { deps, cleanup: previous?.cleanup }; effects.push(() => { slots[index].cleanup?.(); slots[index].cleanup = effect() }) } },
    },
    async api(url, request) {
      const body = JSON.parse(request.body)
      calls.push({ url, body })
      const result = options.api?.(body)
      if (result !== undefined) return result
      if (body.tool === 'list_files') return { status: 'ok', items: [sample] }
      if (body.tool === 'read_file') return { status: 'ok', content: 'synthetic text\r\n', encoding: 'utf-8', truncated: false }
      assert.fail(`Unexpected tool ${body.tool}`)
    },
    render() {
      globalThis.rightWorkbenchTest = h
      if (dirty && !disposed) {
        cursor = 0; dirty = false; tree = component(currentProps)
        const domTab = node => ({ id: node.props.id, focus() { h.focused = node.props.id }, scrollIntoView() {} })
        const tabs = () => nodes(tree).filter(node => node.props?.role === 'tab')
        h.tabListDom = { querySelectorAll: () => tabs().map(domTab), querySelector: () => { const selected = tabs().find(node => node.props['aria-selected']); return selected && domTab(selected) } }
        const list = nodes(tree).find(node => node.props?.role === 'tablist')
        if (list?.props.ref) list.props.ref.current = h.tabListDom
        for (const effect of effects.splice(0)) effect()
      }
      return tree
    },
    update(next) { Object.assign(currentProps, next); dirty = true; h.render() },
    find(predicate) { return nodes(h.render()).find(predicate) },
    button(label) { const button = h.find(node => node.type === 'button' && (node.props['aria-label'] === label || words(node) === label)); assert.ok(button, `button ${label} exists`); return button },
    async click(label) { const button = h.button(label); assert.ok(!button.props.disabled, `button ${label} is enabled`); button.props.onClick(); await h.flush() },
    text() { return words(h.render()) },
    async flush() { for (let index = 0; index < 6; index++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { disposed = true; globalThis.rightWorkbenchTest = h; for (const slot of slots) slot?.cleanup?.() },
  }
  h.render()
  return h
}

// UI does not accept path traversal, drive paths, UNC, empty segments or control
// characters. The backend sandbox remains authoritative for symlinks/ACLs.
for (const value of ['../secret', 'a/../b', '/root', '\\\\server\\share', 'C:' + '\\key', 'a//b', './a', 'a\0b']) assert.equal(workspaceRelativePath(value), null, value)
assert.equal(workspaceRelativePath('中文\\说明.md'), '中文/说明.md')
assert.equal(workspaceRelativePath('.', true), '.')
assert.deepEqual(workspaceEntries([sample, { ...sample, path: '..\\escape' }, { ...sample, path: 'note.txt' }, { path: 'a', type: 'unknown' }]), [sample])
assert.equal(workspaceEntries(null).length, 0)

// No eager filesystem or model call: even opening the panel starts on its home.
{
  const h = harness()
  await h.flush(); assert.equal(h.calls.length, 0)
  assert.equal(h.render().props.hidden, true)
  h.update({ open: true }); await h.flush(); assert.equal(h.calls.length, 0)
  assert.match(h.text(), /工作区文件/)
  assert.doesNotMatch(h.text(), /浏览器|新建终端/)
  await h.click('文件')
  assert.deepEqual(h.calls[0], { url: '/api/tools/execute', body: { conversation_id: 42, workspace: 'synthetic-workspace', permission_mode: 'readonly', tool: 'list_files', arguments: { path: '.' } } })
  const open = h.button('note.txt1 KB').props.onClick
  open(); open(); await h.flush()
  assert.equal(h.calls.filter(call => call.body.tool === 'read_file').length, 1, 'rapid duplicate clicks cannot duplicate the read')
  const read = h.calls.find(call => call.body.tool === 'read_file').body
  assert.deepEqual(read.arguments, { path: 'note.txt', preserve_newlines: true, max_chars: MAX_PREVIEW_CHARS })
  assert.equal(h.find(node => node.type === 'pre').props.children, 'synthetic text\r\n')
  h.update({ open: false }); h.update({ open: true }); await h.flush()
  assert.equal(h.calls.length, 2, 'hide/show retains selected tabs and content, without another read')
  assert.match(h.text(), /synthetic text/)
  await h.click('关闭 note.txt 标签页'); assert.equal(h.find(node => node.type === 'pre'), undefined)
  assert.equal(h.find(node => node.props?.role === 'tab' && words(node) === '文件').props['aria-selected'], true)
  h.dispose()
}

// A closed read cannot overwrite its reopened file, even when it resolves last.
{
  const first = deferred(), second = deferred()
  let reads = 0
  const h = harness({ props: { open: true }, api: body => body.tool === 'read_file' ? (++reads === 1 ? first.promise : second.promise) : undefined })
  await h.click('文件'); await h.click('note.txt1 KB')
  await h.click('关闭 note.txt 标签页'); await h.click('note.txt1 KB')
  second.resolve({ status: 'ok', content: 'current read' }); await h.flush()
  first.resolve({ status: 'ok', content: 'obsolete read' }); await h.flush()
  assert.equal(h.find(node => node.type === 'pre').props.children, 'current read')
  h.dispose()
}

// Per-conversation, workspace and permission changes remount an independent
// scope; a disposed response cannot populate the new scope.
{
  const key = RightWorkbench(props).key
  assert.notEqual(key, RightWorkbench({ ...props, active: { ...props.active, id: 43 } }).key)
  assert.notEqual(key, RightWorkbench({ ...props, workspace: 'different' }).key)
  assert.notEqual(key, RightWorkbench({ ...props, mode: 'full' }).key)
  const pending = deferred()
  const old = harness({ props: { open: true }, api: body => body.tool === 'read_file' ? pending.promise : undefined })
  await old.click('文件'); await old.click('note.txt1 KB'); old.dispose()
  const next = harness({ props: { open: true, active: { ...props.active, id: 43 } } })
  pending.resolve({ status: 'ok', content: 'old workspace content' }); await old.flush(); await next.flush()
  assert.doesNotMatch(next.text(), /old workspace content|note.txt/)
  assert.equal(next.calls.length, 0)
  next.dispose()
}

// Multiple file tabs, bounded count, safe close, and no write-capable dispatch.
{
  const files = Array.from({ length: MAX_PREVIEW_TABS + 1 }, (_, index) => ({ name: `n${index}.txt`, path: `n${index}.txt`, type: 'file', size: 0 }))
  const h = harness({ props: { open: true }, api: body => body.tool === 'list_files' ? { status: 'ok', items: files } : undefined })
  await h.click('文件')
  for (let index = 0; index < MAX_PREVIEW_TABS; index++) { await h.click(`n${index}.txt0 KB`); await h.click('文件') }
  await h.click(`n${MAX_PREVIEW_TABS}.txt0 KB`)
  assert.match(h.text(), /最多同时打开 8 个文件/)
  assert.equal(h.calls.filter(call => call.body.tool === 'read_file').length, MAX_PREVIEW_TABS)
  await h.click('关闭 n0.txt 标签页'); await h.click(`n${MAX_PREVIEW_TABS}.txt0 KB`)
  assert.equal(h.calls.filter(call => call.body.tool === 'read_file').length, MAX_PREVIEW_TABS + 1)
  assert.ok(h.calls.every(call => ['list_files', 'read_file'].includes(call.body.tool)))
  h.dispose()
}

// Broker denial never prompts, retries with approval, or displays old contents.
{
  const h = harness({ props: { open: true }, api: body => body.tool === 'read_file' ? { status: 'confirmation_required', approval_key: 'synthetic-not-for-display' } : undefined })
  await h.click('文件'); await h.click('note.txt1 KB')
  assert.match(h.text(), /授权尚未完成/)
  assert.doesNotMatch(h.text(), /synthetic-not-for-display/)
  assert.equal(h.calls.filter(call => call.body.tool === 'read_file').length, 1)
  h.dispose()
}

// The latest directory selection wins, regardless of response order. Entries
// not accepted from a broker listing cannot produce file reads.
{
  const oldListing = deferred(), newListing = deferred()
  let listings = 0
  const h = harness({ props: { open: true }, api: body => body.tool === 'list_files' ? (++listings === 1 ? oldListing.promise : newListing.promise) : undefined })
  const clickFiles = h.button('文件').props.onClick
  clickFiles(); await h.flush()
  // Simulate a second valid directory refresh before the first render settles.
  // The refresh button is disabled to user clicks; invoking the old callback
  // directly still must not accept the old response after the new one.
  h.button('刷新工作区文件').props.onClick(); await h.flush()
  newListing.resolve({ status: 'ok', items: [{ ...sample, name: 'current.txt', path: 'current.txt' }, { ...sample, name: 'escape', path: '../escape' }] }); await h.flush()
  oldListing.resolve({ status: 'ok', items: [{ ...sample, path: 'obsolete.txt' }] }); await h.flush()
  assert.match(h.text(), /current.txt/); assert.doesNotMatch(h.text(), /obsolete.txt|escape/)
  assert.equal(h.calls.some(call => call.body.tool === 'read_file'), false)
  h.dispose()
}

// Tab keyboard movement and Delete operate on real tab state without invoking
// tools or submitting a chat task.
{
  const h = harness({ props: { open: true } })
  let focused = -1, prevented = false
  const list = h.find(node => node.props?.role === 'tablist')
  list.props.onKeyDown({ key: 'ArrowRight', preventDefault() { prevented = true }, currentTarget: { querySelectorAll: () => Array.from({ length: 4 }, (_, index) => ({ focus() { focused = index } })) } })
  await h.flush()
  assert.ok(prevented); assert.equal(focused, 1)
  await h.click('note.txt1 KB')
  h.find(node => node.props?.role === 'tablist').props.onKeyDown({ key: 'Delete', preventDefault() {}, currentTarget: {} })
  await h.flush()
  assert.equal(h.find(node => node.type === 'pre'), undefined)
  assert.equal(h.calls.filter(call => call.body.tool === 'read_file').length, 1)
  h.dispose()
}

// Delete and arrow keys belong to the focused tab, including its close button,
// not to an unrelated selected file. Closing returns focus to a retained tab.
{
  const files = ['first.txt', 'second.txt'].map(path => ({ name: path, path, type: 'file', size: 0 }))
  const h = harness({ props: { open: true }, api: body => body.tool === 'list_files' ? { status: 'ok', items: files } : undefined })
  await h.click('文件'); await h.click('first.txt0 KB'); await h.click('文件'); await h.click('second.txt0 KB')
  const origin = id => {
    const owner = h.find(node => node.props?.['data-workbench-tab'] === id)
    assert.ok(owner, `keyboard origin belongs to rendered tab ${id}`)
    return { closest(selector) { assert.equal(selector, '[data-workbench-tab]'); return { dataset: { workbenchTab: owner.props['data-workbench-tab'] } } } }
  }
  let prevented = false
  h.find(node => node.props?.role === 'tablist').props.onKeyDown({ key: 'Delete', target: origin('file:first.txt'), currentTarget: h.tabListDom, preventDefault() { prevented = true } })
  await h.flush()
  assert.ok(prevented)
  assert.equal(h.find(node => node.props?.role === 'tab' && words(node) === 'first.txt'), undefined, 'Delete on an inactive close button closes that file, not the selected file')
  assert.equal(h.find(node => node.props?.role === 'tab' && words(node) === 'second.txt').props['aria-selected'], true)
  assert.equal(h.focused, workbenchElementId('file:second.txt'))
  h.find(node => node.props?.role === 'tablist').props.onKeyDown({ key: 'Delete', target: origin('files'), currentTarget: h.tabListDom, preventDefault() { assert.fail('A fixed tab cannot delete the selected file') } })
  await h.flush()
  assert.ok(h.find(node => node.props?.role === 'tab' && words(node) === 'second.txt'))
  h.find(node => node.props?.role === 'tablist').props.onKeyDown({ key: 'ArrowRight', target: origin('start'), currentTarget: h.tabListDom, preventDefault() {} })
  await h.flush()
  assert.equal(h.find(node => node.props?.role === 'tab' && words(node) === '文件').props['aria-selected'], true, 'Arrow navigation is relative to the focused tab')
  await h.click('second.txt')
  await h.click('关闭 second.txt 标签页')
  assert.equal(h.focused, workbenchElementId('files'), 'Closing the final file returns focus to the file listing tab')
  h.dispose()
}

// Size and content bounds are explicit and never silently edit a partial file.
{
  const h = harness({ props: { open: true }, api: body => body.tool === 'read_file' ? { status: 'ok', content: 'x'.repeat(MAX_PREVIEW_CHARS + 1), truncated: false } : undefined })
  await h.click('文件'); await h.click('note.txt1 KB')
  assert.match(h.text(), /内容已截断/)
  assert.equal(h.find(node => node.type === 'pre').props.children.length, MAX_PREVIEW_CHARS)
  assert.equal(h.find(node => node.props?.dangerouslySetInnerHTML), undefined)
  h.dispose()
}

// No user workspace is silently substituted, and the persona remains reachable.
{
  const h = harness({ props: { open: true, active: { ...props.active, workspace: 'other-workspace' } } })
  await h.click('文件'); assert.equal(h.calls.length, 0); assert.match(h.text(), /此工作区的会话/)
  await h.click('夏目心')
  const persona = h.find(node => node.type === 'KokoroPanel')
  assert.ok(persona); assert.equal(persona.props.hasConversation, true)
  h.dispose()
}

// Session diagnostics never render the runtime payload or raw message contents.
{
  const h = harness({ props: { open: true, pendingCount: 2, runtimeEvents: [{ event: 'tool', payload: { api_key: 'synthetic-secret' } }] } })
  await h.click('会话'); assert.match(h.text(), /等待批准/); assert.match(h.text(), /2 项/)
  assert.doesNotMatch(h.text(), /synthetic-secret|api_key/)
  h.dispose()
}

console.log('Right workbench: scoped broker reads, tab lifecycle, late-response fencing, bounded previews, persona access and private diagnostics passed')
