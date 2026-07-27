import { highlightSegments } from '../../shared/highlightText'

export function HighlightedText({ text, terms }: { text: string; terms: string[] }) {
  return <>{highlightSegments(text, terms).map((segment, index) => segment.matched
    ? <mark key={`${index}-${segment.text}`}>{segment.text}</mark>
    : segment.text)}</>
}
