export type MicrophonePermissionState = 'granted' | 'denied' | 'prompt' | 'unknown'

export interface MicrophoneDeviceOption {
  id: string
  label: string
}

interface PermissionStatusLike {
  readonly state: unknown
  addEventListener?: (type: 'change', listener: () => void) => void
  removeEventListener?: (type: 'change', listener: () => void) => void
}

interface PermissionsLike {
  query: (descriptor: PermissionDescriptor) => Promise<PermissionStatusLike>
}

export const WINDOWS_DEFAULT_MICROPHONE_LABEL = 'Windows 默认麦克风'

export function normalizeMicrophonePermission(value: unknown): MicrophonePermissionState {
  return value === 'granted' || value === 'denied' || value === 'prompt' ? value : 'unknown'
}

export function microphonePermissionLabel(state: MicrophonePermissionState): string {
  if (state === 'granted') return '已允许'
  if (state === 'denied') return '已拒绝'
  if (state === 'prompt') return '待询问'
  return '未知'
}

/**
 * WebView2 versions differ in whether `microphone` is accepted by the
 * Permissions API. Unsupported and rejected queries are an explicit unknown;
 * getUserMedia remains the authority when the user starts recording.
 */
export async function observeMicrophonePermission(
  permissions: PermissionsLike | undefined,
  onState: (state: MicrophonePermissionState) => void,
): Promise<() => void> {
  if (!permissions?.query) {
    onState('unknown')
    return () => undefined
  }
  try {
    const status = await permissions.query({ name: 'microphone' as PermissionName })
    const publish = () => onState(normalizeMicrophonePermission(status.state))
    publish()
    status.addEventListener?.('change', publish)
    return () => status.removeEventListener?.('change', publish)
  } catch {
    onState('unknown')
    return () => undefined
  }
}

/** Keep an explicit Windows-default selection instead of guessing inputs[0]. */
export function resolveMicrophoneDeviceId(
  selectedId: string,
  devices: readonly MicrophoneDeviceOption[],
): string {
  return selectedId && devices.some(device => device.id === selectedId) ? selectedId : ''
}

export function microphoneLevelPercent(level: number): number {
  if (!Number.isFinite(level)) return 0
  return Math.round(Math.max(0, Math.min(1, level)) * 100)
}
