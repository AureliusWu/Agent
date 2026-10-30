import type { PermissionMode } from '../types'

interface PermissionSave {
  revision: number
  confirmed: PermissionMode
  pending: number
  tail: Promise<unknown>
}

// Serialize writes per conversation. Request completion order must not become
// permission intent order, and a failed final write rolls back to the last
// confirmed server value, never to another optimistic in-flight choice.
export class ConversationPermissionQueue {
  private saves = new Map<number, PermissionSave>()

  async save(conversationId: number, confirmed: PermissionMode, next: PermissionMode, send: () => Promise<unknown>) {
    let state = this.saves.get(conversationId)
    if (!state) {
      state = { revision: 0, confirmed, pending: 0, tail: Promise.resolve() }
      this.saves.set(conversationId, state)
    }
    const owner = state
    if (!owner.pending) owner.confirmed = confirmed
    owner.pending += 1
    const revision = ++owner.revision
    const request = owner.tail.then(send)
    owner.tail = request.then(() => { owner.confirmed = next }, () => {})
    try {
      await request
      return { ok: true as const, latest: revision === owner.revision, mode: next }
    } catch (error) {
      return { ok: false as const, latest: revision === owner.revision, mode: owner.confirmed, error }
    } finally {
      owner.pending -= 1
    }
  }
}
