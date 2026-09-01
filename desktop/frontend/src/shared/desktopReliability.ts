export interface ConversationOwner { conversationId: number | null; epoch: number }

export class ConversationScope {
  private current: ConversationOwner = { conversationId: null, epoch: 0 }

  select(conversationId: number | null): ConversationOwner {
    if (conversationId !== this.current.conversationId) {
      this.current = { conversationId, epoch: this.current.epoch + 1 }
    }
    return this.capture()
  }

  capture(): ConversationOwner { return { ...this.current } }

  accepts(owner: ConversationOwner): boolean {
    return owner.conversationId === this.current.conversationId && owner.epoch === this.current.epoch
  }
}

export class DesktopEndpointEpoch {
  private identity = ''
  private epoch = -1
  private revision = 0

  observe(status: { epoch: number; port: number | null; ready: boolean }): boolean {
    if (status.epoch < this.epoch) return false
    const identity = `${status.epoch}:${status.port}:${status.ready}`
    if (this.identity === identity) return false
    this.identity = identity
    this.epoch = status.epoch
    this.revision += 1
    return true
  }

  matches(status: { epoch: number; port: number | null; ready: boolean }): boolean {
    return this.identity === `${status.epoch}:${status.port}:${status.ready}`
  }

  invalidate(): void { this.revision += 1 }
  capture(): number { return this.revision }
  accepts(revision: number): boolean { return this.revision === revision }
}

export function needsModelCredential(path: string, omit = false): boolean {
  if (omit) return false
  const url = new URL(path, 'http://siyi.local')
  if (url.searchParams.get('provider_id') === 'ollama') return false
  const pathname = url.pathname
  return pathname === '/api/chat' || pathname === '/api/tasks' || pathname === '/api/provider/health'
    || pathname.endsWith('/compact') || /\/api\/tasks\/[^/]+\/resume$/.test(pathname)
}
