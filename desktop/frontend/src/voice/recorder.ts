export interface VoiceRecording {
  blob: Blob
  mimeType: string
  durationMs: number
}

const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
]

function preferredMimeType(): string {
  if (typeof MediaRecorder === 'undefined') return ''
  return MIME_CANDIDATES.find(type => MediaRecorder.isTypeSupported(type)) || ''
}

export class VoiceRecorder {
  private recorder: MediaRecorder | null = null
  private stream: MediaStream | null = null
  private chunks: Blob[] = []
  private startedAt = 0

  get isRecording(): boolean {
    return this.recorder?.state === 'recording'
  }

  async start(): Promise<void> {
    if (this.isRecording) throw new Error('voice recorder is already running')
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前环境不支持麦克风录音')
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const mimeType = preferredMimeType()
    this.chunks = []
    this.startedAt = performance.now()
    this.recorder = mimeType
      ? new MediaRecorder(this.stream, { mimeType })
      : new MediaRecorder(this.stream)
    this.recorder.addEventListener('dataavailable', event => {
      if (event.data.size > 0) this.chunks.push(event.data)
    })
    this.recorder.start()
  }

  stop(): Promise<VoiceRecording> {
    const recorder = this.recorder
    if (!recorder || recorder.state === 'inactive') return Promise.reject(new Error('voice recorder is not running'))
    return new Promise((resolve, reject) => {
      recorder.addEventListener('error', () => reject(new Error('麦克风录音失败')), { once: true })
      recorder.addEventListener('stop', () => {
        const mimeType = recorder.mimeType || this.chunks[0]?.type || 'application/octet-stream'
        const result = {
          blob: new Blob(this.chunks, { type: mimeType }),
          mimeType,
          durationMs: Math.max(0, Math.round(performance.now() - this.startedAt)),
        }
        this.cleanup()
        resolve(result)
      }, { once: true })
      recorder.stop()
    })
  }

  cancel(): void {
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop()
    this.cleanup()
  }

  private cleanup(): void {
    this.stream?.getTracks().forEach(track => track.stop())
    this.stream = null
    this.recorder = null
    this.chunks = []
    this.startedAt = 0
  }
}
