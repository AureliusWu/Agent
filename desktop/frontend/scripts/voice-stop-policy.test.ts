import assert from 'node:assert/strict'
import {
  isFinalTaskStatus,
  isVoiceStopAggregateSettled,
  resolveVoiceTaskStopAuthority,
} from '../src/voiceStopPolicy.ts'

assert.deepEqual(
  resolveVoiceTaskStopAuthority('voice-task', {
    agent_tasks: [{
      task_id: 'voice-task',
      status: 'cancelled',
      active: false,
      settled: true,
      queue_items_active: 0,
    }],
  }),
  { coveredByVoice: true, status: 'cancelled', settled: true },
  'an exact aggregate entry must prevent a duplicate ordinary task cancel',
)

assert.deepEqual(
  resolveVoiceTaskStopAuthority('voice-task', {
    agent_tasks: [{
      task_id: 'voice-task',
      status: 'cancelled',
      active: false,
      settled: false,
      queue_items_active: 1,
    }],
  }),
  { coveredByVoice: true, status: 'cancelled', settled: false },
  'a final task row must not overrule unsettled queue ownership',
)

assert.equal(isVoiceStopAggregateSettled({
  status: 'CANCELLED',
  sessions: [{ state: 'CANCELLED' }],
  agent_tasks: [{ settled: true, active: false, queue_items_active: 0 }],
  tts: { status: 'CANCELLED', settled: true, unresolved_requests: [] },
}), true)

assert.equal(isVoiceStopAggregateSettled({
  status: 'CANCEL_REQUESTED',
  sessions: [{ state: 'CANCEL_REQUESTED' }],
  agent_tasks: [{ settled: true, active: false, queue_items_active: 0 }],
  tts: { status: 'FAILED' },
}), false, 'resource cleanup failure keeps the aggregate unresolved')

assert.deepEqual(
  resolveVoiceTaskStopAuthority('voice-task', {
    agent_tasks: [{
      task_id: 'voice-task',
      status: 'cancel_requested',
      active: true,
      settled: false,
      queue_items_active: 1,
    }],
  }),
  { coveredByVoice: true, status: 'cancel_requested', settled: false },
  'an unresolved Voice cancellation stays authoritative while the UI polls it',
)

assert.deepEqual(
  resolveVoiceTaskStopAuthority('text-task', {
    agent_tasks: [{ task_id: 'other-task', status: 'cancelled' }],
  }),
  { coveredByVoice: false, status: null, settled: false },
  'an ordinary text task not listed in the aggregate still needs task cancellation',
)

assert.deepEqual(
  resolveVoiceTaskStopAuthority('text-task', { status: 'CANCELLED' }),
  { coveredByVoice: false, status: null, settled: false },
  'a malformed or legacy response cannot claim task-level cancellation authority',
)

assert.equal(isFinalTaskStatus('completed'), true)
assert.equal(isFinalTaskStatus('partially_completed'), true)
assert.equal(isFinalTaskStatus('cancelled'), true)
assert.equal(isFinalTaskStatus('cancel_requested'), false)
assert.equal(isFinalTaskStatus('interrupted'), false)

console.log('Voice stop authority policy tests passed')
