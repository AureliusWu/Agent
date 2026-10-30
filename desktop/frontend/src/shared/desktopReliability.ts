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
    // Health probes can time out without replacing the authenticated core.
    // Only a different generation/endpoint invalidates in-flight results.
    const identity = `${status.epoch}:${status.port}`
    if (this.identity === identity) return false
    this.identity = identity
    this.epoch = status.epoch
    this.revision += 1
    return true
  }

  matches(status: { epoch: number; port: number | null; ready: boolean }): boolean {
    return this.identity === `${status.epoch}:${status.port}`
  }

  invalidate(): void { this.revision += 1 }
  capture(): number { return this.revision }
  accepts(revision: number): boolean { return this.revision === revision }
}

export function isTaskExecution(path: string, method = 'GET'): boolean {
  if (method.toUpperCase() !== 'POST') return false
  const pathname = new URL(path, 'http://siyi.local').pathname
  return pathname === '/api/chat' || pathname === '/api/tasks' || /^\/api\/tasks\/[^/]+\/resume$/.test(pathname)
}

export function needsModelCredential(path: string, omit = false, method = 'GET'): boolean {
  if (omit) return false
  const url = new URL(path, 'http://siyi.local')
  if (url.searchParams.get('provider_id') === 'ollama' || url.searchParams.get('provider_id') === 'mock') return false
  const pathname = url.pathname
  return pathname === '/api/provider/health' || pathname === '/api/provider/models'
    || isTaskExecution(path, method) || (method.toUpperCase() === 'POST' && pathname.endsWith('/compact'))
}
