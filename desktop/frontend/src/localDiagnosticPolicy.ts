/** Independent service observations: failures are not empty success or zero. */
export interface LocalDiagnostic<T> { value: T | null; error: string | null }

export async function readLocalDiagnostic<T>(label: string, read: () => Promise<T>): Promise<LocalDiagnostic<T>> {
  try { return { value: await read(), error: null } }
  catch { return { value: null, error: `${label}读取失败，状态未知；可刷新重试。` } }
}

export function resourceBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return '未知'
  if (value === 0) return '0 B'
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KiB`
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(0)} MiB`
  return `${(value / 1024 ** 3).toFixed(2)} GiB`
}

export function resourceSampleLabel(sampledAt: string | null | undefined, now = Date.now()): string {
  if (!sampledAt || !Number.isFinite(Date.parse(sampledAt))) return '采样时间未知'
  const stamp = Date.parse(sampledAt)
  const stale = now - stamp > 30_000 || stamp > now + 5_000
  return `${stale ? '旧采样，请刷新' : '采样于'} ${new Date(stamp).toLocaleTimeString()}`
}
