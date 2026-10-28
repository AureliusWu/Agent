import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === '@tauri-apps/api/core') return { shortCircuit: true, url: 'data:text/javascript,export const invoke = () => { throw new Error("No native calls in layout test") }' }
    if (specifier.startsWith('.') && context.parentURL?.startsWith('file:')) {
      const url = new URL(specifier, context.parentURL)
      for (const extension of ['.ts', '.tsx']) {
        if (existsSync(fileURLToPath(url) + extension)) return { shortCircuit: true, url: url.href + extension }
      }
    }
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (/\.(svg|png|webp|jpe?g)$/.test(url)) return { shortCircuit: true, format: 'module', source: `export default ${JSON.stringify(url)}` }
    if (url.endsWith('.css')) return { shortCircuit: true, format: 'module', source: 'export default {}' }
    if (url.startsWith('file:') && /\.(ts|tsx)$/.test(url)) {
      return { shortCircuit: true, format: 'module', source: ts.transpileModule(readFileSync(fileURLToPath(url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2023, jsx: ts.JsxEmit.ReactJSX } }).outputText }
    }
    return nextLoad(url, context)
  },
})

globalThis.window = {}
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} }
globalThis.fetch = () => { throw new Error('Layout rendering must never call a model or service') }
const { MainConversationArea } = await import('../src/components/chat/MainConversationArea.tsx')
const { VoiceChatProvider } = await import('../src/components/chat/VoiceChatContext.tsx')
const { AttachmentMenu } = await import('../src/components/chat/AttachmentMenu.tsx')
const { PermissionMenu } = await import('../src/components/settings/PermissionMenu.tsx')
const { ModelReasoningMenu } = await import('../src/components/tasks/ModelReasoningMenu.tsx')
const noop = () => {}
const props = {
  messages: [], pending: [], runtimeEvents: [], input: '', busy: false, stopState: 'idle', error: '', mode: 'ask', reasoningEffort: 'auto', preferredModel: '', defaultModel: 'deepseek-flash', modelOptions: ['deepseek-flash'], hasConversation: true, workspace: '', workspaceOptions: [], orchestrationMode: 'single', queuedItems: [], commands: [], verification: null, usage: null, context: null, recoverable: null, selectedCheckpoint: null, workspaceDrift: false, uncertainOperation: false, endRef: { current: null },
  onInput: noop, onMode: noop, onReasoningEffort: noop, onPreferredModel: noop, onSend: noop, onSteer: noop, onPromoteQueued: noop, onCancelQueued: noop, onStop: noop, onResume: noop, onAbandon: noop, onCheckpoint: noop, onNavigate: noop, onUploadFile: noop, onApprove: noop, onReject: noop, onClearError: noop, onChooseWorkspace: noop, onWorkspaceChange: noop, onOrchestrationMode: noop,
}
function render(overrides = {}) {
  return renderToStaticMarkup(createElement(VoiceChatProvider, { value: { conversationId: 1, boundVoiceSessionId: null, onInterruptTts: noop, onTranscript: noop, onDiscardTranscript: noop } }, createElement(MainConversationArea, { ...props, ...overrides })))
}

const empty = render()
assert.match(empty, /class="main-conversation-area is-empty"/)
assert.match(empty, /conversation-brand/)
assert.match(empty, /默认工作区/)
assert.match(empty, /标准模式/)
assert.doesNotMatch(empty, /主对话与任务区|从一件具体的事开始|conversation-empty/)
assert.match(empty, /data-composer-input="true"/)
assert.doesNotMatch(empty, /class="voice-details"/, 'voice diagnostics do not clutter the primary toolbar')
const controls = ['添加文件或调用指令', '选择权限', '切换模型与推理', '开始录音', '发送']
let previous = -1
for (const label of controls) {
  const index = empty.indexOf(`aria-label="${label}"`)
  assert.ok(index > previous, `toolbar order: ${label}`)
  previous = index
}
const populated = render({ messages: [{ id: 1, role: 'user', content: 'A real conversation remains visible.' }] })
assert.doesNotMatch(populated, /class="main-conversation-area is-empty"|conversation-brand/)
assert.match(populated, /A real conversation remains visible\./)
assert.match(populated, /data-composer-input="true"/)
const busy = render({ busy: true, input: 'next task' })
assert.match(busy, /正在处理当前任务/)
assert.match(busy, /停止/)
assert.match(busy, /排队/)
assert.match(busy, /引导/)
const attachments = renderToStaticMarkup(createElement(AttachmentMenu, { hasConversation: true, onUpload: noop, onCommand: noop, onNavigate: noop, onClose: noop }))
assert.match(attachments, /调用指令/)
assert.doesNotMatch(attachments, /<button(?![^>]*\btype="button")[^>]*>/, 'opening a menu must never implicitly submit a model task')
for (const menu of [
  createElement(PermissionMenu, { mode: 'ask', onSelect: noop }),
  createElement(ModelReasoningMenu, { preferredModel: '', defaultModel: 'deepseek-flash', modelOptions: ['deepseek-flash'], reasoningEffort: 'auto', onPreferredModel: noop, onReasoningEffort: noop }),
]) {
  assert.doesNotMatch(renderToStaticMarkup(menu), /<button(?![^>]*\btype="button")[^>]*>/, 'permission/model selection must never submit the composer')
}
const runtimeSource = readFileSync(new URL('../src/hooks/useAgentChat.ts', import.meta.url), 'utf8')
assert.match(runtimeSource, /orchestrationMode: OrchestrationMode = 'single'/, 'standard single-agent remains the default')
assert.equal((runtimeSource.match(/orchestration_mode: orchestrationMode/g) || []).length, 2, 'normal and queued tasks both honor the selected mode')
const source = readFileSync(new URL('../src/components/chat/MainConversationArea.tsx', import.meta.url), 'utf8')
assert.equal((source.match(/<Composer\s/g) || []).length, 1, 'keep the same Composer instance while the conversation leaves its empty state')
console.log('Chat home layout, toolbar order, stable composer, safe attachment buttons and existing conversation controls passed')
