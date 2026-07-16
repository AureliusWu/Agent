import { invoke } from '@tauri-apps/api/core'

export function isDesktop(): boolean {
  return '__TAURI_INTERNALS__' in window
}

export async function saveDesktopSecret(name: string, value: string): Promise<void> {
  if (!isDesktop()) throw new Error('网页端密钥由后端环境变量管理')
  await invoke('set_secret', { name, value })
}

export async function hasDesktopSecret(name: string): Promise<boolean> {
  if (!isDesktop()) return false
  return Boolean(await invoke<string | null>('get_secret', { name }))
}

export async function getDesktopSecret(name: string): Promise<string | null> {
  if (!isDesktop()) return null
  return invoke<string | null>('get_secret', { name })
}

export async function deleteDesktopSecret(name: string): Promise<void> {
  if (!isDesktop()) throw new Error('网页端密钥由后端环境变量管理')
  await invoke('delete_secret', { name })
}
