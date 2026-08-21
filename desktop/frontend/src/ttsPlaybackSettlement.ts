export interface PlaybackSettlement {
  promise: Promise<void>
  complete: () => void
  fail: (error: Error) => void
  interrupt: () => void
  settled: () => boolean
}

export interface PlaybackInterruptionGate {
  interrupt: () => void
  interrupted: () => boolean
  throwIfInterrupted: () => void
}

/**
 * Keeps an interruption received during asynchronous audio preparation from
 * being lost before a browser player exists.  The caller installs `interrupt`
 * before its first await, then checks the gate before it starts playback.
 */
export function createPlaybackInterruptionGate(onInterrupt: () => void): PlaybackInterruptionGate {
  let wasInterrupted = false
  return {
    interrupt: () => {
      if (wasInterrupted) return
      wasInterrupted = true
      onInterrupt()
    },
    interrupted: () => wasInterrupted,
    throwIfInterrupted: () => {
      if (wasInterrupted) throw new Error('TTS playback was interrupted')
    },
  }
}

export function createPlaybackSettlement(): PlaybackSettlement {
  let done = false
  let resolvePromise: () => void = () => undefined
  let rejectPromise: (error: Error) => void = () => undefined
  const promise = new Promise<void>((resolve, reject) => {
    resolvePromise = resolve
    rejectPromise = reject
  })
  const settle = (error?: Error) => {
    if (done) return
    done = true
    if (error) rejectPromise(error)
    else resolvePromise()
  }
  return {
    promise,
    complete: () => settle(),
    fail: error => settle(error),
    interrupt: () => settle(new Error('TTS playback was interrupted')),
    settled: () => done,
  }
}

export async function waitForPlayback(
  settlement: PlaybackSettlement,
  startPlayback: () => Promise<void>,
): Promise<void> {
  // Attach the interruption handler before invoking play(). Browsers are
  // allowed to leave HTMLMediaElement.play() pending while they negotiate the
  // device/decoder, so awaiting that Promise directly can strand the queue.
  const playbackStarted = Promise.resolve().then(startPlayback)
  await Promise.race([playbackStarted, settlement.promise])
  await settlement.promise
}
