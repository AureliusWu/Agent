import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Render and invoke the real production component. Only React scheduling and
// the HTTP boundary are simulated; all paths and receipts are synthetic.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx = (type,props,key) => ({type,props,key}); export const jsxs = jsx; export const Fragment = "Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useRef', 'useEffect'].map(name => `export const ${name} = (...args) => globalThis.fileWorkbenchTest.hooks.${name}(...args)`).join('\n'))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['ChevronRight', 'File', 'Files', 'Folder', 'Settings2', 'Upload'].map(name => `export const ${name} = "${name}"`).join('\n'))}` }
    if (specifier === '../../api') return { shortCircuit: true, url: 'data:text/javascript,export const api = (...args) => globalThis.fileWorkbenchTest.api(...args); export const apiFetch = api;' }
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
const { FileOperationsPanel } = await import('../src/components/workspace/FileOperationsPanel.tsx')
const { FilesPanel } = await import('../src/components/workspace/FilesPanel.tsx')
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes }); return { promise, resolve } }
const sameDeps = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]))
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
function harness(options = {}) {
  const slots = [], effects = [], calls = []
  let cursor = 0, dirty = true, disposed = false, tree
  const props = { conversationId: 42, workspace: 'synthetic-workspace', mode: 'agent', selected: '', selectedFiles: [], onChanged() {}, ...options.props }
  const h = {
    calls, props,
    hooks: {
      useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { value: typeof initial === 'function' ? initial() : initial }; return [slots[i].value, value => { slots[i].value = typeof value === 'function' ? value(slots[i].value) : value; dirty = true }] },
      useRef(initial) { const i = cursor++; if (!(i in slots)) slots[i] = { current: initial }; return slots[i] },
      useEffect(effect, deps) { const i = cursor++; if (!slots[i] || !sameDeps(slots[i].deps, deps)) { const old = slots[i]; slots[i] = { deps, cleanup: old?.cleanup }; effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = effect() }) } },
    },
    async api(url, request) {
      const body = request?.body ? JSON.parse(request.body) : undefined
      calls.push({ url, body })
      const custom = options.api?.(url, body)
      if (custom !== undefined) return custom
      if (url === '/api/tools/execute') {
        if (body.tool === 'list_file_changes') return { status: 'ok', changes: [] }
        if (body.tool === 'file_metadata') return { status: 'ok', type: 'file', version_token: 'file:synthetic', size: 8 }
        if (body.tool === 'read_file') return { status: 'ok', content: 'hello', version_token: 'file:synthetic', truncated: false }
        if (body.tool === 'file_diff') return { status: 'ok', diff: '+synthetic' }
        if (body.tool === 'file_batch') return { status: 'ok', plan_hash: 'a'.repeat(64), operation_id: body.arguments.operation_id, results: [] }
        return { status: 'ok' }
      }
      return { items: [], total: 0, limit: 25, offset: 0, truncated: false }
    },
    render() { globalThis.fileWorkbenchTest = h; if (dirty && !disposed) { dirty = false; cursor = 0; tree = (options.component || FileOperationsPanel)(props); for (const effect of effects.splice(0)) effect() } return tree },
    update(next) { Object.assign(props, next); dirty = true; h.render() },
    button(label) { const found = nodes(h.render()).find(node => node.type === 'button' && words(node) === label); assert.ok(found, `button ${label} exists`); return found },
    async click(label) { const button = h.button(label); assert.equal(Boolean(button.props.disabled), false, `button ${label} enabled`); button.props.onClick(); await h.flush() },
    set(label, value) { const element = nodes(h.render()).find(node => node.type === 'label' && words(node).startsWith(label)); assert.ok(element, `label ${label} exists`); const input = nodes(element).find(node => ['input', 'textarea', 'select'].includes(node.type)); input.props.onChange({ target: { value } }); h.render() },
    text() { return words(h.render()) },
    async flush() { for (let i = 0; i < 6; i++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
    dispose() { disposed = true; for (const slot of slots) slot?.cleanup?.() },
  }
  globalThis.fileWorkbenchTest = h
  globalThis.window = { confirm: () => true }
  h.render()
  return h
}

// Synchronous lock, not React's next-render busy state, must reject double clicks.
{
  const pending = deferred()
  const h = harness({ props: { selected: 'readme.txt' }, api: (_url, body) => body?.tool === 'read_file' ? pending.promise : undefined })
  await h.flush()
  const click = h.button('读取文件版本').props.onClick
  click(); click()
  assert.equal(h.calls.filter(call => call.body?.tool === 'read_file').length, 1, 'two callbacks before a render must issue one request')
  h.dispose(); pending.resolve({ status: 'ok', content: 'late response must be ignored', version_token: 'late' }); await h.flush()
}

// Preview/execution share a stable ID plus server hash. Mounting never replays.
{
  const h = harness()
  await h.flush()
  assert.equal(h.calls.some(call => call.body?.tool === 'file_batch'), false)
  h.set('文件路径', 'new.txt'); h.set('文本内容', 'synthetic text')
  await h.click('加入计划并预览差异')
  await h.click('预检批次')
  const oldExecute = h.button('执行 1 项').props.onClick
  await h.click('执行 1 项')
  oldExecute(); await h.flush()
  const batches = h.calls.filter(call => call.body?.tool === 'file_batch').map(call => call.body.arguments)
  assert.equal(batches.length, 2, 'an already-rendered execution callback cannot issue old operations under a new ID')
  assert.match(batches[0].operation_id, /^[\da-f-]{36}$/)
  assert.equal(batches[1].operation_id, batches[0].operation_id)
  assert.equal(batches[1].expected_plan_hash, 'a'.repeat(64))
  assert.equal(batches[0].dry_run, true); assert.equal(batches[1].dry_run, false)
  h.dispose()
}

// All write paths remain inaccessible in readonly, even invoking callbacks directly.
{
  const h = harness({ props: { mode: 'readonly', selectedFiles: ['a.txt'] } })
  await h.flush()
  assert.equal(h.button('生成多选计划与差异').props.disabled, true)
  h.button('生成多选计划与差异').props.onClick(); await h.flush()
  assert.equal(h.calls.some(call => call.body?.tool === 'file_batch' || call.body?.tool === 'file_metadata'), false)
  assert.match(h.text(), /只读模式禁止修改/)
  h.dispose()
}

// Selection changes fence a late read from a different file.
{
  const pending = deferred()
  const h = harness({ props: { selected: 'first.txt' }, api: (_url, body) => body?.tool === 'read_file' ? pending.promise : undefined })
  await h.flush(); h.button('读取文件版本').props.onClick()
  h.update({ selected: 'second.txt' }); await h.flush()
  pending.resolve({ status: 'ok', content: 'stale-first-file', version_token: 'old' }); await h.flush()
  const textarea = nodes(h.render()).find(node => node.type === 'textarea')
  assert.equal(textarea.props.value, '')
  h.dispose()
}

// Truncation is a hard stop; never submit a replacement based on partial text.
{
  const h = harness({ props: { selectedFiles: ['a.txt'] }, api: (_url, body) => body?.tool === 'read_file' ? { status: 'ok', content: 'hello', version_token: 'v', truncated: true } : undefined })
  await h.flush(); h.set('批量操作', 'replace'); h.set('查找文本', 'hello'); h.set('替换为', 'world')
  await h.click('生成多选计划与差异')
  assert.match(h.text(), /截断/)
  assert.equal(h.calls.some(call => call.body?.tool === 'file_diff' || call.body?.tool === 'file_batch'), false)
  const read = h.calls.find(call => call.body?.tool === 'read_file')
  assert.equal(read.body.arguments.preserve_newlines, true)
  h.dispose()
}

// A target edit after preview must clear approval/hash and obtain a fresh ID.
{
  const h = harness({ props: { selectedFiles: ['a.txt'] } })
  await h.flush(); h.set('名称前缀', 'new_'); await h.click('生成多选计划与差异'); await h.click('预检批次')
  const first = h.calls.find(call => call.body?.tool === 'file_batch').body.arguments.operation_id
  const target = nodes(h.render()).find(node => node.props?.['aria-label'] === '第 1 项目标路径')
  target.props.onChange({ target: { value: 'other.txt' } }); await h.flush()
  assert.equal(h.button('执行 1 项').props.disabled, true)
  await h.click('预检批次')
  const previews = h.calls.filter(call => call.body?.tool === 'file_batch')
  assert.notEqual(previews[1].body.arguments.operation_id, first)
  assert.equal(previews[1].body.arguments.operations[0].arguments.destination, 'other.txt')
  h.dispose()
}

// A refused grant cannot be reused by a later operation and is not exposed in diagnostics.
{
  const h = harness({ props: { mode: 'ask' }, api: (_url, body) => body?.tool === 'file_batch' ? { status: 'confirmation_required', approval_key: 'test-grant' } : undefined })
  await h.flush(); h.set('文件路径', 'new.txt'); h.set('文本内容', 'text'); await h.click('加入计划并预览差异')
  globalThis.window.confirm = () => false
  await h.click('预检批次')
  assert.equal(h.calls.filter(call => call.body?.tool === 'file_batch').length, 1)
  assert.doesNotMatch(h.text(), /test-grant/)
  assert.equal(h.button('执行 1 项').props.disabled, true)
  h.dispose()
}

// Durable records only restore via a single backend batch tool, with a fresh grant.
{
  const id = 'synthetic-batch-0001'
  const batch = { operation_id: id, status: 'committed', steps: [{ operation_id: `${id}:0`, step_index: 0, operation: 'file.write', state: 'committed', change_id: 'synthetic-change', before: { 'a.txt': {} } }] }
  const h = harness({ props: { mode: 'ask' }, api: (url, body) => {
    if (url.startsWith('/api/file-transactions?')) return { items: [batch], total: 1 }
    if (url.startsWith(`/api/file-transactions/${id}`)) return batch
    if (body?.tool === 'undo_file_batch') return body.approval_tokens.length ? { status: 'ok', recovery_status: 'restored' } : { status: 'confirmation_required', approval_key: 'fresh-undo-grant' }
  } })
  await h.flush()
  assert.equal(h.calls.some(call => call.body?.tool === 'file_batch' || call.body?.tool === 'undo_file_batch'), false, 'mounting metadata does not replay or undo')
  await h.click('恢复最近批次')
  const undo = h.calls.filter(call => call.body?.tool?.startsWith('undo'))
  assert.equal(undo.length, 2)
  assert.deepEqual(undo.map(call => call.body.tool), ['undo_file_batch', 'undo_file_batch'])
  assert.deepEqual(undo[0].body.arguments, { operation_id: id })
  assert.deepEqual(undo[0].body.approval_tokens, [])
  assert.deepEqual(undo[1].body.approval_tokens, ['fresh-undo-grant'])
  assert.doesNotMatch(h.text(), /fresh-undo-grant/)
  h.dispose()
}

// Old approval responses after scope disposal must not prompt or dispatch writes.
{
  const pending = deferred()
  const h = harness({ api: (_url, body) => body?.tool === 'file_batch' ? pending.promise : undefined })
  await h.flush(); h.set('文件路径', 'new.txt'); h.set('文本内容', 'text'); await h.click('加入计划并预览差异')
  h.button('预检批次').props.onClick(); h.dispose()
  globalThis.window.confirm = () => { assert.fail('disposed scope cannot request authorization') }
  pending.resolve({ status: 'confirmation_required', approval_key: 'stale-grant' }); await h.flush()
  assert.equal(h.calls.filter(call => call.body?.tool === 'file_batch').length, 1)
}

// Existing Files view: filtering + selection cap and remount isolation, not a new route.
{
  const active = { id: 42 }
  const props = { active, workspace: 'synthetic-workspace', mode: 'agent' }
  const scope = FilesPanel(props)
  assert.notEqual(scope.key, FilesPanel({ ...props, mode: 'readonly' }).key)
  assert.notEqual(scope.key, FilesPanel({ ...props, workspace: 'other' }).key)
  assert.notEqual(scope.key, FilesPanel({ ...props, active: { id: 43 } }).key)
  const h = harness({ component: scope.type, props, api: (_url, body) => body?.tool === 'list_files' ? { status: 'ok', items: Array.from({ length: 51 }, (_, i) => ({ path: `a${i}.txt`, name: `a${i}.txt`, type: 'file', size: 1 })) } : undefined })
  await h.flush(); await h.click('选择筛选结果')
  assert.match(h.text(), /超过 50/)
  assert.equal(nodes(h.render()).filter(node => node.type === 'input' && node.props.type === 'checkbox' && node.props.checked).length, 0)
  h.set('筛选当前目录', 'a1'); await h.click('选择筛选结果')
  assert.equal(nodes(h.render()).filter(node => node.type === 'input' && node.props.type === 'checkbox' && node.props.checked).length, 11)
  await h.click('清空选择')
  assert.equal(nodes(h.render()).filter(node => node.type === 'input' && node.props.type === 'checkbox' && node.props.checked).length, 0)
  h.dispose()
}
// Literal replacement passes untouched CRLF/terminal newline through to diff and plan.
{
  const h = harness({ props: { selectedFiles: ['a.txt'] }, api: (_url, body) => body?.tool === 'read_file' ? { status: 'ok', content: 'hello\r\nkept\r\n', version_token: 'v-crlf', truncated: false } : undefined })
  await h.flush(); h.set('批量操作', 'replace'); h.set('查找文本', 'hello'); h.set('替换为', 'world')
  await h.click('生成多选计划与差异'); await h.click('预检批次')
  const diff = h.calls.find(call => call.body?.tool === 'file_diff')
  assert.equal(diff.body.arguments.content, 'world\r\nkept\r\n')
  const preview = h.calls.find(call => call.body?.tool === 'file_batch')
  assert.equal(preview.body.arguments.operations[0].arguments.content, 'world\r\nkept\r\n')
  assert.equal(preview.body.arguments.operations[0].arguments.expected_version_token, 'v-crlf')
  h.dispose()
}

// A changed multi-selection discards pending metadata and does not build an old plan.
{
  const pending = deferred()
  const h = harness({ props: { selectedFiles: ['first.txt'] }, api: (_url, body) => body?.tool === 'file_metadata' ? pending.promise : undefined })
  await h.flush(); h.set('名称前缀', 'new_'); h.button('生成多选计划与差异').props.onClick()
  h.update({ selectedFiles: ['second.txt'] }); await h.flush()
  pending.resolve({ status: 'ok', type: 'file', version_token: 'v', size: 1 }); await h.flush()
  assert.doesNotMatch(h.text(), /待执行计划/)
  h.dispose()
}

// Recovery pagination keeps unknown capacity explicit and only issues read requests.
{
  const h = harness({ props: { mode: 'readonly' }, api: url => {
    if (!url.startsWith('/api/file-recovery?')) return undefined
    const offset = Number(new URL(url, 'http://synthetic.test').searchParams.get('offset'))
    return { items: [{ change_id: `change-${offset}`, operation: 'write_file', created_at: 'synthetic', paths: ['a.txt'], status: offset ? 'restored' : 'available', retained_bytes: null, capacity_status: 'unknown' }], total: 26, limit: 25, offset, truncated: false }
  } })
  await h.flush()
  assert.match(h.text(), /容量未知/)
  assert.equal(h.button('撤销此项').props.disabled, true)
  await h.click('下一页恢复记录')
  assert.ok(h.calls.some(call => call.url.endsWith('offset=25')))
  assert.equal(h.button('下一页恢复记录').props.disabled, true)
  await h.click('上一页恢复记录')
  assert.equal(h.calls.some(call => call.body?.tool?.startsWith('undo')), false)
  h.dispose()
}
// Reconciliation stays observational, including readonly mode and ambiguous effects.
{
  const id = 'synthetic-batch-0002'
  const batch = { operation_id: id, status: 'needs_attention', steps: [{ operation_id: `${id}:0`, step_index: 0, operation: 'file.move', state: 'effect_started', before: { 'a.txt': {} } }] }
  const h = harness({ props: { mode: 'readonly' }, api: url => {
    if (url.startsWith('/api/file-transactions?')) return { items: [batch], total: 1 }
    if (url.endsWith('/reconcile')) return { operation_id: id, status: 'needs_attention', steps: [{ step_index: 0, observed_state: 'needs_attention' }], workspace_modified: false }
    if (url.startsWith(`/api/file-transactions/${id}`)) return batch
  } })
  await h.flush(); await h.click('查看步骤'); await h.click('核对现场（不修改文件）')
  assert.match(h.text(), /核对结果：需要人工核对/)
  assert.equal(h.calls.some(call => call.url === '/api/tools/execute'), false)
  assert.equal(h.button('请求恢复此批次').props.disabled, true)
  h.dispose()
}

// A version conflict stops before execution and provides a human-readable next step.
{
  const h = harness({ api: (_url, body) => body?.tool === 'file_batch' ? { status: 'error', success: false, error_code: 'batch_preflight_failed', cause_error_code: 'version_conflict', failed_index: 0 } : undefined })
  await h.flush(); h.set('文件路径', 'new.txt'); h.set('文本内容', 'text'); await h.click('加入计划并预览差异'); await h.click('预检批次')
  assert.match(h.text(), /重新读取文件/)
  assert.match(h.text(), /未启动文件修改/)
  assert.match(h.text(), /失败步骤：第 1 项/)
  assert.equal(h.button('执行 1 项').props.disabled, true)
  assert.equal(h.calls.some(call => call.body?.tool === 'file_batch' && !call.body.arguments.dry_run), false)
  h.dispose()
}
console.log('File workbench production component: 15 selection/concurrency/frozen-plan/readonly/late-response/recovery/conflict scenarios passed')
