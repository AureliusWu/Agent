import { invoke } from '@tauri-apps/api/core'

export function isDesktop(): boolean {
  return '__TAURI_INTERNALS__' in window
}

export async function saveDesktopSecret(name: string, value: string): Promise<void> {
  if (!isDesktop()) throw new Error('司忆密钥管理仅支持 Windows 桌面应用')
  await invoke('set_secret', { name, value })
}

export async function hasDesktopSecret(name: string): Promise<boolean> {
  if (!isDesktop()) throw new Error('司忆密钥管理仅支持 Windows 桌面应用')
  return Boolean(await invoke<string | null>('get_secret', { name }))
}

export async function getDesktopSecret(name: string): Promise<string | null> {
  if (!isDesktop()) throw new Error('司忆密钥管理仅支持 Windows 桌面应用')
  return invoke<string | null>('get_secret', { name })
}

export async function deleteDesktopSecret(name: string): Promise<void> {
  if (!isDesktop()) throw new Error('司忆密钥管理仅支持 Windows 桌面应用')
  await invoke('delete_secret', { name })
}
