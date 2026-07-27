import assert from 'node:assert/strict'
import { mergeReasoningSummaries, publicReasoningSummary } from '../src/reasoningEvents.ts'

const sentinel = 'PRIVATE_CHAIN_OF_THOUGHT_SENTINEL'
const summary = '正在分析任务目标、约束和可用能力。'

assert.equal(publicReasoningSummary('model.reasoning.delta', { delta: sentinel }), null)
assert.equal(publicReasoningSummary('reasoning.summary', { summary }), summary)
const visibleTimeline = mergeReasoningSummaries(undefined, summary)
assert.equal(mergeReasoningSummaries(visibleTimeline, summary), summary)
assert.equal(visibleTimeline.includes(sentinel), false)
console.log('Reasoning UI boundary tests passed: raw delta rejected, public summary rendered')
