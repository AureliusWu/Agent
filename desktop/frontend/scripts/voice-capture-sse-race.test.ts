import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { VoiceSseSessionFence } from '../src/voiceSseSessionFence.ts'

const fence = new VoiceSseSessionFence()
const first = fence.begin('voice-session-first')
assert.equal(fence.owns(first, 'voice-session-first'), true)
assert.equal(fence.markTerminal(first, 'voice-session-first'), true)
assert.equal(fence.isTerminal(first), true)
assert.equal(
  fence.shouldFailClosed(first, 'voice-session-first', {
    inputActive: true,
    startPending: false,
    reconnectLimitReached: false,
  }),
  false,
  'an explicit terminal event must not be treated as a broken stream',
)

const second = fence.begin('voice-session-second')
assert.equal(fence.owns(first, 'voice-session-first'), false, 'a prior stream must lose ownership when a new session begins')
assert.equal(fence.isTerminal(first), false, 'a prior terminal marker must not survive into a new stream owner')
assert.equal(fence.markTerminal(first, 'voice-session-first'), false, 'a buffered old terminal event must not mark a new session terminal')
assert.equal(fence.clear(first), false, 'a stale stream must not clear a new stream owner')
assert.equal(fence.owns(second, 'voice-session-second'), true)
assert.equal(
  fence.shouldFailClosed(second, 'voice-session-second', {
    inputActive: true,
    startPending: false,
    reconnectLimitReached: false,
  }),
  true,
  'an unexpected disconnect while input is active must fail closed immediately',
)
assert.equal(
  fence.shouldFailClosed(second, 'voice-session-second', {
    inputActive: false,
    startPending: true,
    reconnectLimitReached: false,
  }),
  true,
  'an unexpected disconnect during permission/session setup must fence a later microphone acquisition',
)
assert.equal(
  fence.shouldFailClosed(second, 'voice-session-second', {
    inputActive: false,
    startPending: false,
    reconnectLimitReached: false,
  }),
  false,
  'after input release, one bounded reconnect remains allowed for the non-capture lifecycle',
)
assert.equal(
  fence.shouldFailClosed(second, 'voice-session-second', {
    inputActive: false,
    startPending: false,
    reconnectLimitReached: true,
  }),
  true,
  'non-capture reconnect exhaustion must still cancel the current session',
)

const hookPath = fileURLToPath(new URL('../src/hooks/useVoiceCapture.ts', import.meta.url))
const controlPath = fileURLToPath(new URL('../src/components/chat/VoiceInputControl.tsx', import.meta.url))
// Keep this source-level regression test portable across the Windows checkout
// and CI environments that use LF line endings.
const source = (await readFile(hookPath, 'utf8')).replace(/\r\n/g, '\n')
const controlSource = (await readFile(controlPath, 'utf8')).replace(/\r\n/g, '\n')

assert.ok(
  source.includes('const voiceEventsFenceRef = useRef(new VoiceSseSessionFence())'),
  'terminal state must be scoped to the current voice-session stream owner, not a global boolean',
)
assert.ok(
  source.includes('const handleVoiceEvent = useCallback((owner: VoiceSseStreamToken, event: VoiceStreamEvent) => {'),
  'SSE event handling must know which stream delivered an event',
)
assert.ok(
  source.includes(
    'sessionId !== owner.voiceSessionId ||\n' +
    '      !voiceEventsFenceRef.current.owns(owner, voiceSessionIdRef.current)',
  ),
  'events must match the active stream owner and active capture session before mutating UI state',
)
assert.ok(
  source.includes("if (event.event === 'VOICE_SESSION_COMPLETED') voiceEventsFenceRef.current.markTerminal(owner, voiceSessionIdRef.current)"),
  'only the matching completed session may mark its stream terminal',
)
assert.ok(
  source.includes('const ownsStream = () => (') &&
    source.includes('voiceEventsControllerRef.current === controller') &&
    source.includes('voiceEventsFenceRef.current.owns(owner, voiceSessionIdRef.current)'),
  'reconnect/fail-closed work must keep ownership of controller, stream owner and capture session',
)
assert.ok(
  source.includes('voiceEventsFenceRef.current.isTerminal(owner)'),
  'a normal SSE close must only be accepted as terminal for its own stream owner',
)

const failClosedOffset = source.indexOf('// Fail closed:')
const ownershipCheckOffset = source.indexOf('if (!ownsStream()) return', failClosedOffset)
const cancellationOperationOffset = source.indexOf('const cancellationOperation = operationRef.current', failClosedOffset)
const cancelOffset = source.indexOf('await cancelRef.current()', failClosedOffset)
const uiGuardOffset = source.indexOf('operationRef.current === cancellationOperation + 1', failClosedOffset)
const inputActiveOffset = source.indexOf('const inputActive = Boolean(')
const failClosedDecisionOffset = source.indexOf('const mustFailClosed = voiceEventsFenceRef.current.shouldFailClosed(')
assert.ok(failClosedOffset >= 0 && ownershipCheckOffset > failClosedOffset, 'non-terminal stream failure must re-check ownership before cancelling')
assert.ok(cancellationOperationOffset > ownershipCheckOffset && cancelOffset > cancellationOperationOffset, 'fail-closed cancellation must be ordered after ownership capture')
assert.ok(uiGuardOffset > cancelOffset, 'a stale stream must not overwrite UI state after a newer operation starts')
assert.ok(inputActiveOffset >= 0 && failClosedDecisionOffset > inputActiveOffset && failClosedDecisionOffset < failClosedOffset, 'live microphone input must make an SSE disconnect fail closed before retrying')

assert.ok(source.includes('if (pendingStartRef.current) void cancel()'), 'PTT/shortcut early release must cancel a pending start')
assert.ok(controlSource.includes('onPointerUp={endPushToTalk}') && controlSource.includes('onPointerCancel={endPushToTalk}'), 'PTT pointer release and cancellation must both stop capture')
assert.ok(controlSource.includes('capture.stop()'), 'PTT release must delegate to the hook stop fence')
assert.ok(source.includes("if (event.key !== 'Escape' || (!voiceSessionIdRef.current && !pendingStartRef.current)) return"), 'Escape must cancel an active or pending voice start')
assert.ok(source.includes('if (document.hidden) stopForPrivacy()'), 'window minimization/hidden state must stop capture')
assert.ok(source.includes('if (!focused) stopForPrivacy()'), 'native window focus loss must stop capture')

console.log('Voice SSE session-isolation regression test passed')
