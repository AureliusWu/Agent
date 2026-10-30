import type { FileItem } from '../types'

export const MAX_PREVIEW_TABS = 8
export const MAX_PREVIEW_CHARS = 40_000
export const MAX_PREVIEW_BYTES = 2_000_000

export interface WorkspacePreview {
  path: string
  content: string
  loading: boolean
  truncated: boolean
  encoding: string
  error: string
}

// This is a UI guard, not a replacement for the broker's canonical-path sandbox.
export function workspaceRelativePath(value: string, allowRoot = false): string | null {
  const path = value.replaceAll('\\', '/')
  if (allowRoot && path === '.') return path
  if (!path || path.startsWith('/') || path.includes(':') || [...path].some(character => character.charCodeAt(0) < 32) || path.split('/').some(part => !part || part === '.' || part === '..')) return null
  return path
}

export function workspaceEntries(value: unknown): FileItem[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  return value.slice(0, 500).flatMap(raw => {
    if (!raw || typeof raw !== 'object' || typeof raw.path !== 'string' || !['file', 'directory'].includes(raw.type)) return []
    const path = workspaceRelativePath(raw.path)
    if (!path || seen.has(path)) return []
    seen.add(path)
    return [{ path, name: path.split('/').pop()!, type: raw.type as FileItem['type'], ...(typeof raw.size === 'number' && Number.isFinite(raw.size) && raw.size >= 0 ? { size: raw.size } : {}) }]
  })
}

export function previewTabId(path: string): string { return `file:${path}` }
export function workbenchElementId(tab: string): string { return `siyi-workbench-${encodeURIComponent(tab)}` }
