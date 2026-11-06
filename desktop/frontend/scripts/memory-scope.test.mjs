import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const inline = source => ({ shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` })
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return inline('export const jsx=(type,props)=>({type,props});export const jsxs=jsx;export const Fragment="Fragment"')
    if (specifier === 'react') return inline(['useState', 'useEffect', 'useMemo', 'useCallback'].map(name => `export const ${name}=(...args)=>globalThis.memoryScopeTest.${name}(...args);`).join(''))
    if (specifier === 'lucide-react') return inline(['Ban', 'Brain', 'Check', 'Download', 'Pencil', 'Plus', 'Save', 'Trash2', 'Upload', 'X'].map(name => `export const ${name}="${name}";`).join(''))
    if (specifier === '../../api' && context.parentURL?.endsWith('/MemoryManager.tsx')) return inline('export const api=(...args)=>globalThis.memoryScopeTest.api(...args);export const apiFetch=api')
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) {
      return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText }
    }
    return nextLoad(url, context)
  },
})

const { MemoryManager } = await import('../src/components/memory/MemoryManager.tsx')
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.props?.children)] : []
const words = value => Array.isArray(value) ? value.map(words).join('') : value && typeof value === 'object' ? words(value.props?.children) : value == null || typeof value === 'boolean' ? '' : String(value)
const depsEqual = (left, right) => left && left.length === right.length && left.every((value, i) => Object.is(value, right[i]))
const row = (id, namespace, key, category = 'decision') => ({ id, namespace, key, content: key, category, tags: [], stale_reasons: [], effective_confidence: 1, owner_api: 'scoped', editable_via_current_api: true })
const nextTurn = () => new Promise(resolve => setImmediate(resolve))

function harness() {
  const requests = [], slots = [], effects = []
  let cursor = 0, workspace = 'workspace-a', tree
  globalThis.memoryScopeTest = {
    api(path, options) { return new Promise((resolve, reject) => requests.push({ path, options, resolve, reject })) },
    useState(initial) {
      const index = cursor++
      slots[index] ??= { value: typeof initial === 'function' ? initial() : initial }
      return [slots[index].value, value => { slots[index].value = typeof value === 'function' ? value(slots[index].value) : value }]
    },
    useMemo(value, deps) {
      const index = cursor++
      if (!depsEqual(slots[index]?.deps, deps)) slots[index] = { value: value(), deps }
      return slots[index].value
    },
    useCallback(callback, deps) { return this.useMemo(() => callback, deps) },
    useEffect(effect, deps) {
      const index = cursor++
      const previous = slots[index]
      if (depsEqual(previous?.deps, deps)) return
      slots[index] = { deps }
      effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = effect() })
    },
  }
  return {
    requests,
    render(nextWorkspace = workspace, flush = true) {
      workspace = nextWorkspace
      cursor = 0
      tree = MemoryManager({ workspace })
      if (flush) this.flush()
      return words(tree)
    },
    flush() { for (const effect of effects.splice(0)) effect() },
    tab(label) { nodes(tree).find(item => item.type === 'button' && words(item) === label).props.onClick() },
    filter(value) { nodes(tree).find(item => item.type === 'select').props.onChange({ target: { value } }) },
    draftKey() { return nodes(tree).find(item => item.props?.placeholder === 'build.command').props },
  }
}

const h = harness()
h.render()
h.requests.at(-1).resolve([row(1, 'personal', 'PERSONAL_ONLY')])
await nextTurn()
assert.match(h.render(), /PERSONAL_ONLY/)
h.tab('项目记忆')
assert.doesNotMatch(h.render('workspace-a', false), /PERSONAL_ONLY/, 'V17-UI-MEMORY-SCOPE: hide previous namespace before the new GET starts')
h.flush()
const firstProject = h.requests.at(-1)
firstProject.resolve([row(2, 'project', 'PROJECT_A_ONLY')])
await nextTurn()
assert.match(h.render(), /PROJECT_A_ONLY/)
h.draftKey().onChange({ target: { value: 'project.draft' } })
h.render()
assert.equal(h.draftKey().value, 'project.draft')
h.tab('我的记忆')
assert.doesNotMatch(h.render('workspace-a', false), /PROJECT_A_ONLY/, 'do not render project data under the personal heading')
assert.equal(h.draftKey().value, '', 'a draft does not carry into a different namespace')
h.flush()
const pendingPersonal = h.requests.at(-1)
h.tab('项目记忆')
h.render()
const pendingA = h.requests.at(-1)
assert.doesNotMatch(h.render('workspace-b', false), /PROJECT_A_ONLY/, 'hide prior workspace before its replacement GET')
h.flush()
const pendingB = h.requests.at(-1)
assert.equal(pendingPersonal.options.signal.aborted, true)
assert.equal(pendingA.options.signal.aborted, true)
pendingB.resolve([row(3, 'project', 'B_DECISION'), row(4, 'project', 'B_ARCHITECTURE', 'architecture'), row(6, 'personal', 'WRONG_NAMESPACE')])
await nextTurn()
assert.match(h.render(), /B_DECISION/)
assert.doesNotMatch(h.render(), /WRONG_NAMESPACE/, 'a response row from a different namespace cannot render')
pendingA.resolve([row(5, 'project', 'LATE_A_ONLY')])
pendingPersonal.reject(new Error('LATE_PERSONAL_ERROR'))
await nextTurn()
assert.match(h.render(), /B_DECISION/)
assert.doesNotMatch(h.render(), /LATE_A_ONLY|LATE_PERSONAL_ERROR/)
h.filter('architecture')
assert.match(h.render(), /B_ARCHITECTURE/)
assert.doesNotMatch(h.render(), /B_DECISION/)
assert.doesNotMatch(h.render('', false), /B_ARCHITECTURE/, 'closing the workspace hides project rows immediately')
h.flush()
assert.match(h.requests.at(-1).path, /workspace=&namespace=personal$/)

console.log('Memory scope component races passed')
