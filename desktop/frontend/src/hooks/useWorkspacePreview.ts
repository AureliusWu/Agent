import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import type { Conversation, FileItem, PermissionMode } from '../types'
import { fileOperationError } from '../shared/fileOperationPolicy'
import type { FileToolResult } from '../shared/fileOperationPolicy'
import { MAX_PREVIEW_BYTES, MAX_PREVIEW_CHARS, MAX_PREVIEW_TABS, workspaceEntries, workspaceRelativePath } from '../shared/rightWorkbench'
import type { WorkspacePreview } from '../shared/rightWorkbench'

// The owner is keyed by conversation + workspace + permission. Hiding the panel
// does not unmount it, while disposing a scope invalidates all outstanding reads.
export function useWorkspacePreview(active: Conversation | null, workspace: string, mode: PermissionMode, enabled: boolean) {
  const [entries, setEntries] = useState<FileItem[]>([])
  const [directory, setDirectory] = useState('.')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [files, setFiles] = useState<WorkspacePreview[]>([])
  const mounted = useRef(true)
  const sequence = useRef(0)
  const listing = useRef(0)
  const loaded = useRef(false)
  const directoryRef = useRef('.')
  const entriesRef = useRef<FileItem[]>([])
  const filesRef = useRef<WorkspacePreview[]>([])
  const reads = useRef(new Map<string, number>())
  const canBrowse = Boolean(active && workspace && active.workspace === workspace)

  useEffect(() => {
    mounted.current = true
    const inFlight = reads.current
    return () => { mounted.current = false; loaded.current = false; listing.current += 1; inFlight.clear() }
  }, [])

  const execute = useCallback((tool: 'list_files' | 'read_file', arguments_: Record<string, unknown>) => {
    if (!active || !workspace || active.workspace !== workspace) throw new Error('请先选择工作区会话。')
    return api<FileToolResult>('/api/tools/execute', { method: 'POST', body: JSON.stringify({ conversation_id: active.id, workspace, permission_mode: mode, tool, arguments: arguments_ }) })
  }, [active, workspace, mode])

  const loadDirectory = useCallback(async (next = directoryRef.current) => {
    if (!mounted.current || !canBrowse) return
    const path = workspaceRelativePath(next, true)
    if (!path) { setError('仅支持当前工作区内的相对路径。'); return }
    const request = ++listing.current
    loaded.current = true
    directoryRef.current = path
    entriesRef.current = []
    setDirectory(path); setEntries([]); setLoading(true); setError('')
    try {
      const result = await execute('list_files', { path })
      if (!mounted.current || request !== listing.current) return
      if (result.status !== 'ok') throw new Error(fileOperationError(result))
      const items = workspaceEntries(result.items)
      entriesRef.current = items; setEntries(items)
    } catch (caught) {
      if (mounted.current && request === listing.current) setError((caught as Error).message.slice(0, 500))
    } finally {
      if (mounted.current && request === listing.current) setLoading(false)
    }
  }, [canBrowse, execute])

  useEffect(() => { if (enabled && !loaded.current) void loadDirectory('.') }, [enabled, loadDirectory])

  function updateFiles(next: WorkspacePreview[]) { filesRef.current = next; setFiles(next) }

  async function readFile(path: string) {
    if (!mounted.current || !canBrowse || reads.current.has(path)) return
    const current = filesRef.current.find(file => file.path === path)
    if (!current) return
    const request = ++sequence.current
    reads.current.set(path, request)
    updateFiles(filesRef.current.map(file => file.path === path ? { ...file, loading: true, error: '' } : file))
    try {
      const result = await execute('read_file', { path, preserve_newlines: true, max_chars: MAX_PREVIEW_CHARS })
      if (!mounted.current || reads.current.get(path) !== request) return
      if (result.status !== 'ok') throw new Error(fileOperationError(result))
      if (typeof result.content !== 'string') throw new Error('未收到文本内容，无法预览此文件。')
      if (result.content.includes('\0')) throw new Error('此文件不是可预览的纯文本，请使用合适的本地应用打开。')
      const content = result.content
      updateFiles(filesRef.current.map(file => file.path === path ? { ...file, loading: false, content: content.slice(0, MAX_PREVIEW_CHARS), truncated: Boolean(result.truncated) || content.length > MAX_PREVIEW_CHARS, encoding: typeof result.encoding === 'string' ? result.encoding : '' } : file))
    } catch (caught) {
      if (mounted.current && reads.current.get(path) === request) updateFiles(filesRef.current.map(file => file.path === path ? { ...file, loading: false, error: (caught as Error).message.slice(0, 500) } : file))
    } finally {
      if (reads.current.get(path) === request) reads.current.delete(path)
    }
  }

  function openFile(item: FileItem): boolean {
    if (!mounted.current || !canBrowse) return false
    const path = workspaceRelativePath(item.path)
    if (!path || !entriesRef.current.some(entry => entry.type === 'file' && entry.path === path)) return false
    if (filesRef.current.some(file => file.path === path)) return true
    if (filesRef.current.length >= MAX_PREVIEW_TABS) { setError(`最多同时打开 ${MAX_PREVIEW_TABS} 个文件，请先关闭部分标签页。`); return false }
    const listed = entriesRef.current.find(entry => entry.path === path)!
    if ((listed.size ?? 0) > MAX_PREVIEW_BYTES) { setError('文件超过 2 MB，暂不提供侧栏文本预览。'); return false }
    updateFiles([...filesRef.current, { path, content: '', loading: true, truncated: false, encoding: '', error: '' }])
    setError('')
    void readFile(path)
    return true
  }

  function closeFile(path: string) {
    if (!mounted.current) return
    reads.current.delete(path)
    updateFiles(filesRef.current.filter(file => file.path !== path))
  }

  return { entries, directory, loading, error, files, canBrowse, loadDirectory, openFile, closeFile, reloadFile: readFile }
}
