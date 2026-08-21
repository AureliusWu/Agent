import assert from 'node:assert/strict'
import { dispatchVoiceTranscript } from '../src/voiceTranscriptDispatch.ts'

let resolveAgent: (() => void) | null = null
let started = false
let completed = false
const agentTerminal = new Promise<void>(resolve => { resolveAgent = resolve })

dispatchVoiceTranscript('voice text', async content => {
  assert.equal(content, 'voice text')
  started = true
  await agentTerminal
  completed = true
}, error => { throw error })

assert.equal(started, true, 'auto-send must begin immediately')
assert.equal(completed, false, 'capture cleanup must not await Agent terminal state')
resolveAgent?.()
await agentTerminal
await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(completed, true)

let observed: unknown = null
dispatchVoiceTranscript('failure', async () => { throw new Error('submit failed') }, error => { observed = error })
await new Promise(resolve => setTimeout(resolve, 0))
assert.equal((observed as Error).message, 'submit failed')

console.log('Voice transcript dispatch regression test passed')
