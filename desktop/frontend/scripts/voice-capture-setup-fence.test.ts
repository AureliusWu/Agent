import assert from 'node:assert/strict'
import { VoiceCaptureSetupFence, resumeAudioContextWithTimeout } from '../src/voiceCaptureSetupFence.ts'

function fakeStream() {
  const stops = [0, 0]
  return {
    stops,
    stream: {
      getTracks: () => stops.map((_, index) => ({ stop: () => { stops[index] += 1 } })),
    },
  }
}

function fakeContext(resume: () => Promise<void>) {
  let closes = 0
  return {
    get closes() { return closes },
    context: {
      resume,
      close: async () => { closes += 1 },
    },
  }
}

const pending = new VoiceCaptureSetupFence()
const first = pending.begin(1, 'voice-first')
const firstStream = fakeStream()
assert.equal(pending.registerStream(first, firstStream.stream), true)
const firstContext = fakeContext(() => new Promise(() => undefined))
assert.equal(pending.registerAudioContext(first, firstContext.context), true)
assert.equal(pending.owns(first, 1, 'voice-first'), true)

// Escape/focus-loss/unmount/cancel all converge on release(): microphone
// tracks and a resume-blocked AudioContext are released synchronously.
assert.equal(pending.release(first), true)
assert.deepEqual(firstStream.stops, [1, 1])
await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(firstContext.closes, 1)
assert.equal(pending.hasOwner(), false)

// A getUserMedia result arriving after cancellation is stopped at the point
// where it tries to register; it never becomes an unowned live stream.
const lateStream = fakeStream()
assert.equal(pending.registerStream(first, lateStream.stream), false)
assert.deepEqual(lateStream.stops, [1, 1])

// One misbehaving browser track must not prevent the remaining tracks or
// AudioContext from being released.
const throwing = pending.begin(4, 'voice-throwing-track')
let laterTrackStops = 0
assert.equal(pending.registerStream(throwing, {
  getTracks: () => [
    { stop: () => { throw new Error('already gone') } },
    { stop: () => { laterTrackStops += 1 } },
  ],
}), true)
const throwingContext = fakeContext(async () => undefined)
assert.equal(pending.registerAudioContext(throwing, throwingContext.context), true)
assert.equal(pending.release(throwing), true)
assert.equal(laterTrackStops, 1)
await new Promise(resolve => setTimeout(resolve, 0))
assert.equal(throwingContext.closes, 1)

// Replacing an operation fences stale resources and stale completion cannot
// clear the new operation's owner.
const second = pending.begin(2, 'voice-second')
const third = pending.begin(3, 'voice-third')
assert.equal(pending.owns(second, 2, 'voice-second'), false)
assert.equal(pending.complete(second), false)
assert.equal(pending.owns(third, 3, 'voice-third'), true)
pending.release(third)

// A browser that leaves AudioContext.resume() pending forever is bounded.
const stuckContext = fakeContext(() => new Promise(() => undefined))
await assert.rejects(
  resumeAudioContextWithTimeout(stuckContext.context, 5),
  error => error instanceof DOMException && error.name === 'AbortError',
)

console.log('Voice capture setup ownership regression test passed')
