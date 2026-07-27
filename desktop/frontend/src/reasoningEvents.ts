export function publicReasoningSummary(event: string, payload: Record<string, unknown>): string | null {
  if (event !== 'reasoning.summary') return null
  const summary = String(payload.summary || '').trim()
  return summary || null
}

export function mergeReasoningSummaries(existing: string | undefined, summary: string): string {
  const summaries = (existing || '').split('\n').map(item => item.trim()).filter(Boolean)
  return summaries.includes(summary) ? summaries.join('\n') : [...summaries, summary].join('\n')
}
