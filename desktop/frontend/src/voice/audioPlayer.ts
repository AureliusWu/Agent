export class VoiceAudioPlayer {
  private audio: HTMLAudioElement | null = null
  private objectUrl: string | null = null

  get isPlaying(): boolean {
    return Boolean(this.audio && !this.audio.paused && !this.audio.ended)
  }

  async play(blob: Blob): Promise<void> {
    this.stop()
    this.objectUrl = URL.createObjectURL(blob)
    this.audio = new Audio(this.objectUrl)
    try {
      await this.audio.play()
    } catch (error) {
      this.stop()
      throw error
    }
    this.audio.addEventListener('ended', () => this.stop(), { once: true })
  }

  stop(): void {
    if (this.audio) {
      this.audio.pause()
      this.audio.src = ''
      this.audio = null
    }
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl)
      this.objectUrl = null
    }
  }
}
