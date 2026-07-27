export interface HighlightSegment { text: string; matched: boolean }

export function highlightSegments(text: string, terms: string[]): HighlightSegment[] {
  const normalized = [...new Set(terms.map(term => term.trim()).filter(Boolean))]
  if (!normalized.length) return [{ text, matched: false }]
  const escaped = normalized.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).sort((a, b) => b.length - a.length)
  const pattern = new RegExp(`(${escaped.join('|')})`, 'gi')
  return text.split(pattern).filter(Boolean).map(part => ({
    text: part,
    matched: normalized.some(term => part.toLocaleLowerCase('zh-CN') === term.toLocaleLowerCase('zh-CN')),
  }))
}
