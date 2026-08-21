export interface StreamBuffer {
  text: string
  inCodeFence: boolean
  scan: number
}

export const TTS_SENTENCE_MAX_CHARS = 160

export function splitStreaming(state: StreamBuffer, delta: string): string[] {
  state.text += delta
  const sentences: string[] = []
  let cursor = 0
  let index = state.scan
  while (index < state.text.length) {
    if (state.text.startsWith('```', index) || state.text.startsWith('~~~', index)) {
      state.inCodeFence = !state.inCodeFence
      index += 3
      continue
    }
    const hardLimit = index - cursor + 1 >= TTS_SENTENCE_MAX_CHARS
    if (!state.inCodeFence && (/[。！？!?；;\n]/.test(state.text[index]) || hardLimit)) {
      const sentence = state.text.slice(cursor, index + 1).trim()
      if (sentence) sentences.push(sentence)
      cursor = index + 1
    }
    index += 1
  }
  state.text = state.text.slice(cursor)
  state.scan = Math.max(0, index - cursor)
  return sentences
}
