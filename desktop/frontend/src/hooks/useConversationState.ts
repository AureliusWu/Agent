import { useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import type { ConversationScope } from '../shared/desktopReliability'

/** Every runtime value and asynchronous setter belongs to one conversation epoch. */
export function useConversationState<T>(scope: ConversationScope, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const owner = scope.capture()
  const create = () => typeof initial === 'function' ? (initial as () => T)() : initial
  const [entry, setEntry] = useState(() => ({ owner, value: create() }))
  const current = entry.owner.conversationId === owner.conversationId && entry.owner.epoch === owner.epoch
  const value = current ? entry.value : create()
  const setValue: Dispatch<SetStateAction<T>> = update => {
    if (!scope.accepts(owner)) return
    setEntry(previous => {
      if (!scope.accepts(owner)) return previous
      const old = previous.owner.conversationId === owner.conversationId && previous.owner.epoch === owner.epoch ? previous.value : create()
      return { owner, value: typeof update === 'function' ? (update as (value: T) => T)(old) : update }
    })
  }
  return [value, setValue]
}

/** Mutable resources also have separate ref objects: old closures can never mutate a new session's controller. */
export function useConversationRef<T>(scope: ConversationScope, initial: T): { current: T } {
  const owner = scope.capture()
  const entry = useRef({ owner, ref: { current: initial } })
  if (entry.current.owner.conversationId !== owner.conversationId || entry.current.owner.epoch !== owner.epoch) {
    entry.current = { owner, ref: { current: initial } }
  }
  return entry.current.ref
}
