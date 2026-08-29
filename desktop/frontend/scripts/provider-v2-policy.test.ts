import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const apiSource = fs.readFileSync(fileURLToPath(new URL('../src/api.ts', import.meta.url)), 'utf8')
const providerPanel = fs.readFileSync(fileURLToPath(new URL('../src/components/providers/DeepSeekProviderPanel.tsx', import.meta.url)), 'utf8')
const localPanel = fs.readFileSync(fileURLToPath(new URL('../src/components/providers/LocalAiPanel.tsx', import.meta.url)), 'utf8')

assert.match(apiSource, /const pathname = apiPathname\(path\)/, 'credential routing must normalize query-bearing URLs to pathname')
assert.match(apiSource, /pathname === '\/api\/provider\/health'/, 'provider health must receive desktop credential after pathname normalization')
assert.match(apiSource, /X-Siyi-Omit-Model-Credential/, 'local providers need an explicit client-side credential omission fence')

assert.match(providerPanel, /api<LocalModelChoice\[]>\('\/api\/local-models\/models'\)/, 'provider settings must load installed models')
assert.match(providerPanel, /installedModels\.filter\(model => model\.installed\)\.map/, 'every installed model must be selectable')
assert.doesNotMatch(providerPanel, /<option value="qwen3:4b"/, 'qwen3:4b must not remain the only rendered model option')
assert.match(providerPanel, /value="openai_compatible"/, 'OpenAI-compatible must be available through the unified settings surface')

assert.match(localPanel, /provider_id: 'ollama'/, 'Local AI settings must persist provider selection')
assert.match(localPanel, /model: model\.model_id/, 'Local AI settings must persist the selected installed model')

console.log('Provider v2 frontend policy tests passed')
