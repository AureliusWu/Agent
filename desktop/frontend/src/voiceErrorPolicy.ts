function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' ? value as Record<string, unknown> : null
}

function detail(error: unknown): Record<string, unknown> | null {
  const candidate = record(error)
  return record(candidate?.detail) || candidate
}

export function voiceErrorCode(error: unknown): string {
  const code = detail(error)?.code
  return typeof code === 'string' ? code : ''
}

function memoryAmount(value: unknown): string | null {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) return null
  // Show the observed amount without turning missing telemetry into zero.
  return `${(value / 1024 ** 3).toFixed(2)} GiB`
}

function resourceShortage(error: unknown, kind: 'ram' | 'vram'): string {
  const resource = record(detail(error)?.resource)
  const available = resource?.kind === kind ? memoryAmount(resource.available_bytes) : null
  const minimum = resource?.kind === kind ? memoryAmount(resource.minimum_available_bytes) : null
  const label = kind === 'ram' ? '内存' : '显存'
  const amounts = available && minimum ? `（本次检查可用 ${available}，安全下限 ${minimum}）` : ''
  const recovery = kind === 'ram'
    ? '请保存工作并关闭占用内存较多的程序，释放内存后重新录音。'
    : '请释放显存，或在语音设置中切换到 CPU 后重新录音。'
  return `可用${label}不足${amounts}，本次语音输入未完成。${recovery}无需重启司忆，仍可使用文字输入。`
}

export function userFacingVoiceError(error: unknown): string {
  const code = voiceErrorCode(error)
  if (code === 'RESOURCE_RAM_PRESSURE') return resourceShortage(error, 'ram')
  if (code === 'RESOURCE_VRAM_PRESSURE') return resourceShortage(error, 'vram')
  const name = error instanceof DOMException ? error.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return '麦克风权限被拒绝。请在 Windows 和司忆的权限设置中允许麦克风后重试。'
  if (name === 'NotFoundError') return '没有发现可用麦克风。请连接设备后重试。'
  if (name === 'NotReadableError') return '麦克风正在被其他程序占用。请关闭占用程序后重试。'
  if (isCancelledTranscriptionError(error)) return '语音输入已取消。'
  if (code === 'STT_PERMISSION_DENIED') return '本地语音文件或模型访问被拒绝，请检查本地转写设置。'
  if (code === 'STT_TRANSCRIPTION_FAILED') return '本地语音转写失败，请重新录音后重试。'
  return error instanceof Error && error.message ? error.message : '语音输入失败，请重试。'
}

function isCancelledTranscriptionError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') return true
  return ['STT_ALREADY_CANCELLED', 'STT_CANCELLED', 'VOICE_SESSION_CANCELLED'].includes(voiceErrorCode(error))
}

/** A FAILED event releases hardware too; that alone is not user cancellation. */
export function voiceTranscriptionFailureDisposition(
  error: unknown,
  captureCancelled: boolean,
  terminalFailed: boolean,
): 'cancelled' | 'preserve_failure' | 'error' {
  if (terminalFailed) return voiceErrorCode(error) && !isCancelledTranscriptionError(error) ? 'error' : 'preserve_failure'
  return captureCancelled || isCancelledTranscriptionError(error) ? 'cancelled' : 'error'
}

export function preserveVoiceFailure(current: string): string {
  return current || '语音输入未完成，请检查麦克风或本地转写状态后重试。'
}
