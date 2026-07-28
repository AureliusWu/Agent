import { invoke } from '@tauri-apps/api/core'
import { isDesktop } from './secrets'

export type BackendPhase = 'starting' | 'ready' | 'restarting' | 'error' | 'stopped'

export interface DesktopBackendHealth {
  port: number | null
  pid: number | null
  ready: boolean
  phase: BackendPhase
  error: string | null
  restart_count: number
  log_directory: string
}

const sleep = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))

export async function getDesktopBackendStatus(): Promise<DesktopBackendHealth | null> {
  if (!isDesktop()) return null
  return invoke<DesktopBackendHealth>('backend_status')
}

export async function waitForDesktopBackend(timeoutMilliseconds = 30_000): Promise<DesktopBackendHealth> {
  if (!isDesktop()) throw new Error('当前不是桌面运行环境')
  const deadline = Date.now() + timeoutMilliseconds
  let last: DesktopBackendHealth | null = null
  while (Date.now() < deadline) {
    last = await getDesktopBackendStatus()
    if (last?.ready && last.port) return last
    if (last?.phase === 'error' && !last.pid) break
    await sleep(180)
  }
  const logHint = last?.log_directory ? `；日志：${last.log_directory}` : ''
  throw new Error(`${last?.error || '本地核心未能在规定时间内就绪'}${logHint}`)
}

export async function restartDesktopBackend(): Promise<DesktopBackendHealth> {
  if (!isDesktop()) throw new Error('当前环境不是司忆 Windows 桌面应用')
  await invoke<DesktopBackendHealth>('restart_backend')
  return waitForDesktopBackend()
}
