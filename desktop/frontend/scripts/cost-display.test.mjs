import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

// Execute real production components with synthetic HTTP and React hooks.
// This does not contact a provider, alter settings or use a user database.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === 'react/jsx-runtime') return { shortCircuit: true, url: 'data:text/javascript,export const jsx=(type,props,key)=>({type,props,key});export const jsxs=jsx;export const Fragment="Fragment";' }
    if (specifier === 'react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['useState', 'useEffect'].map(name => `export const ${name}=(...args)=>globalThis.costDisplayTest.hooks.${name}(...args);`).join(''))}` }
    if (specifier === 'lucide-react') return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(['Activity', 'Coins', 'Gauge', 'Network', 'ChevronDown', 'ChevronRight', 'GitBranch', 'History', 'ShieldCheck'].map(name => `export const ${name}="${name}";`).join(''))}` }
    if (specifier === '../../api') return { shortCircuit: true, url: 'data:text/javascript,export const api=(...args)=>globalThis.costDisplayTest.api(...args);' }
    if (specifier === '../shared/PanelHeader') return { shortCircuit: true, url: 'data:text/javascript,export const PanelHeader="PanelHeader";' }
    if (specifier.endsWith('.css')) return { shortCircuit: true, url: 'data:text/javascript,export {};' }
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
const { costEstimateLabel, costEstimateDetail } = await import('../src/shared/costPolicy.ts')
const { UsagePanel } = await import('../src/components/providers/UsagePanel.tsx')
const { AuditPanel } = await import('../src/components/settings/AuditPanel.tsx')
const words = tree => tree == null || typeof tree === 'boolean' ? '' : Array.isArray(tree) ? tree.map(words).join('') : typeof tree === 'object' ? words(tree.props?.children) : String(tree)
const nodes = tree => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
function harness(component, response) {
  const slots = [], effects = [], calls = []
  let cursor = 0, dirty = true, tree
  const h = {
    calls,
    hooks: {
      useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = { value: initial }; return [slots[index].value, value => { slots[index].value = value; dirty = true }] },
      useEffect(effect) { const index = cursor++; if (!(index in slots)) { slots[index] = {}; effects.push(effect) } },
    },
    async api(url, options) { calls.push({ url, options }); return structuredClone(response) },
    render() { globalThis.costDisplayTest = h; if (dirty) { cursor = 0; dirty = false; tree = component(); for (const effect of effects.splice(0)) effect() } return tree },
    text() { return words(h.render()) },
    async flush() { for (let index = 0; index < 4; index++) { await new Promise(resolve => setImmediate(resolve)); h.render() } },
  }
  h.render()
  return h
}
const task = estimate => ({
  id: 'synthetic-cost-task', prompt: 'Synthetic public cost fixture', status: 'completed',
  created_at: '2026-10-01T00:00:00Z', model_calls: 1, input_tokens: 17, output_tokens: 3,
  cache_hits: 0, cache_misses: 0, phase_costs: {}, checkpoints: [], operations: [], plan: null,
  task_intelligence: { dependencies: [], requirements: [], acceptance_conditions: [] },
  professional_trace: { roles: [], messages: [] }, repair_runs: [], agent_runs: [], file_locks: [],
  performance: { aggregates: {}, traces: [] }, provider_policy: null, model_runs: [], skill_runs: [],
  data_flows: [], security_snapshots: [], tool_runs: [], ...estimate,
})
const estimates = [
  { cost_status: 'unknown', estimated_cost_usd: null, known_cost_usd: 0, unknown_cost_requests: 1, pending_cost_requests: 0 },
  { cost_status: 'partial', estimated_cost_usd: null, known_cost_usd: 0.125, unknown_cost_requests: 1, pending_cost_requests: 0 },
  { cost_status: 'known', estimated_cost_usd: 0, known_cost_usd: 0, unknown_cost_requests: 0, pending_cost_requests: 0 },
  { cost_status: 'known', estimated_cost_usd: 0.125, known_cost_usd: 0.125, unknown_cost_requests: 0, pending_cost_requests: 0 },
  { cost_status: 'known', estimated_cost_usd: 0.00000001, known_cost_usd: 0.00000001, unknown_cost_requests: 0, pending_cost_requests: 0 },
  { cost_status: 'unknown', estimated_cost_usd: null, known_cost_usd: 0, unknown_cost_requests: 0, pending_cost_requests: 1 },
]
for (const estimate of estimates) {
  const usage = harness(UsagePanel, { period_days: 30, totals: { requests: 1, input_tokens: 17, output_tokens: 3, total_tokens: 20, cached_input_tokens: 0, uncached_input_tokens: 17, cache_write_tokens: 0, cache_hit_rate: 0, average_duration_ms: 722, ...estimate }, models: [], daily: [] })
  await usage.flush()
  assert.ok(usage.text().includes(costEstimateLabel(estimate)))
  assert.ok(usage.text().includes(costEstimateDetail(estimate)))
  assert.equal(usage.calls.length, 1)
  assert.equal(usage.calls[0].options, undefined, 'usage mount is read-only')
  const audit = harness(AuditPanel, [task(estimate)])
  await audit.flush()
  nodes(audit.render()).find(node => node.type === 'button').props.onClick()
  assert.ok(audit.text().includes(costEstimateLabel(estimate, 6)))
  assert.ok(audit.text().includes(costEstimateDetail(estimate, 6)))
  assert.equal(audit.calls.length, 1)
  if (estimate.cost_status !== 'known') {
    assert.doesNotMatch(usage.text(), /\$0\.0000(?!\d)/)
    assert.doesNotMatch(audit.text(), /\$0\.000000(?!\d)/)
  }
}
for (const value of [null, undefined, NaN, Infinity, -1, '0']) {
  assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: value }), '费用未知')
}
assert.equal(costEstimateLabel({ estimated_cost_usd: 0 }), '费用未知', 'legacy zero without explicit known status is not known')
assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: 0, pending_cost_requests: 1 }), '费用未知')
assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: 0, unknown_cost_requests: 1 }), '费用未知')
assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: 0 }), '$0.0000')
assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: 0.00000001 }), '<$0.0001')
assert.equal(costEstimateLabel({ cost_status: 'known', estimated_cost_usd: 0.00000001 }, 6), '<$0.000001')
console.log('V16 cost display: 12 real component scenarios and unknown/zero/small/partial/pending boundaries passed')
