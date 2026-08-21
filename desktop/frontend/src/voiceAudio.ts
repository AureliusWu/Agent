export const VOICE_SAMPLE_RATE = 16_000
export const VOICE_CHANNELS = 1
export const VOICE_BITS_PER_SAMPLE = 16

/**
 * Encodes the one canonical audio format accepted by the local STT endpoint.
 * This deliberately does not retain the samples after creating the Blob.
 */
export function encodeMonoPcmWav(samples: Float32Array, sampleRate = VOICE_SAMPLE_RATE): Blob {
  if (!Number.isInteger(sampleRate) || sampleRate <= 0) throw new Error('Invalid voice sample rate')
  const bytesPerSample = VOICE_BITS_PER_SAMPLE / 8
  const dataBytes = samples.length * bytesPerSample
  const buffer = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buffer)

  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + dataBytes, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, VOICE_CHANNELS, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * VOICE_CHANNELS * bytesPerSample, true)
  view.setUint16(32, VOICE_CHANNELS * bytesPerSample, true)
  view.setUint16(34, VOICE_BITS_PER_SAMPLE, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, dataBytes, true)

  for (let index = 0; index < samples.length; index += 1) {
    const value = Number.isFinite(samples[index]) ? Math.max(-1, Math.min(1, samples[index])) : 0
    view.setInt16(44 + index * bytesPerSample, value < 0 ? value * 0x8000 : value * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

/**
 * Decodes the browser recorder output with Web Audio, then renders a single
 * 16 kHz mono track. There is intentionally no native or external-recorder
 * fallback: this is the only desktop capture conversion path.
 */
export async function mediaBlobToVoiceWav(blob: Blob, decodingContext: AudioContext): Promise<Blob> {
  if (!blob.size) throw new Error('No microphone audio was captured')
  if (typeof OfflineAudioContext === 'undefined') throw new Error('Web Audio offline rendering is unavailable')
  const encoded = await blob.arrayBuffer()
  const decoded = await decodingContext.decodeAudioData(encoded.slice(0))
  const frameCount = Math.max(1, Math.ceil(decoded.duration * VOICE_SAMPLE_RATE))
  const renderer = new OfflineAudioContext(VOICE_CHANNELS, frameCount, VOICE_SAMPLE_RATE)
  const source = renderer.createBufferSource()
  source.buffer = decoded
  source.connect(renderer.destination)
  source.start()
  const rendered = await renderer.startRendering()
  return encodeMonoPcmWav(rendered.getChannelData(0), rendered.sampleRate)
}

function writeAscii(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index))
}
