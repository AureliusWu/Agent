import { useEffect, useRef, useState } from 'react'
import { ChevronRight, File, Files, Folder, Settings2, Upload } from 'lucide-react'
import { api, apiFetch } from '../../api'
import type { Conversation, FileItem, PermissionMode } from '../../types'
import { PanelHeader } from '../shared/PanelHeader'
import { FileOperationsPanel } from './FileOperationsPanel'
import { LatestRequest } from '../../shared/latestRequest'
import { canConfirmFileOperation, fileOperationError } from '../../shared/fileOperationPolicy'
import type { FileToolResult } from '../../shared/fileOperationPolicy'
import '../../styles/panels.css'

interface Props { active: Conversation | null; workspace: string; mode: PermissionMode }
export function FilesPanel(props: Props) {
  return <ScopedFilesPanel key={`${props.active?.id ?? 'none'}:${props.workspace}:${props.mode}`} {...props}/>
}

function ScopedFilesPanel({ active, workspace, mode }: Props) {
  const [items, setItems] = useState<FileItem[]>([])
  const [path, setPath] = useState('.')
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const mounted = useRef(true)
  const requests = useRef(new LatestRequest())
  useEffect(() => { const gate = requests.current; mounted.current = true; return () => { mounted.current = false; gate.begin() } }, [])

  async function load(next = path) {
    if (!active) return
    const request = requests.current.begin()
    try {
      const result = await api<FileToolResult & { items?: FileItem[] }>('/api/tools/execute', { method: 'POST', body: JSON.stringify({ conversation_id: active.id, workspace, permission_mode: mode, tool: 'list_files', arguments: { path: next } }) })
      if (!mounted.current || !requests.current.isLatest(request)) return
      if (result.status !== 'ok') throw new Error(fileOperationError(result))
      setItems(result.items || []); setPath(next); setError('')
    } catch (caught) { if (mounted.current && requests.current.isLatest(request)) setError((caught as Error).message) }
  }
  // The parent key remounts this scope for every conversation, workspace, or permission change.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void load('.') }, [])

  async function upload(file: globalThis.File) {
    if (!active || mode === 'readonly' || uploading) return
    const target = path === '.' ? file.name : `${path}/${file.name}`
    const send = async (token?: string) => {
      const form = new FormData(); form.append('file', file)
      const query = new URLSearchParams({ workspace, path: target, permission_mode: mode, conversation_id: String(active.id) })
      if (token) query.set('approval_token', token)
      return apiFetch(`/api/files/upload?${query}`, { method: 'POST', body: form })
    }
    setUploading(true); setError('')
    try {
      let response = await send()
      let result = await response.json() as FileToolResult
      if (!mounted.current) return
      if (canConfirmFileOperation(mode, result)) {
        if (!window.confirm(`上传 ${file.name} 到 ${target}？`)) return
        response = await send(result.approval_key)
        result = await response.json() as FileToolResult
      }
      if (!mounted.current) return
      if (!response.ok || result.status !== 'ok') throw new Error(fileOperationError(result))
      await load()
    } catch (caught) { if (mounted.current) setError((caught as Error).message) }
    finally { if (mounted.current) { setUploading(false); if (fileRef.current) fileRef.current.value = '' } }
  }

  return <section className="content-panel"><PanelHeader icon={<Files/>} title="工作区文件" subtitle={workspace}/>
    <div className="panel-toolbar"><button className="primary" disabled={!active || mode === 'readonly' || uploading} onClick={() => fileRef.current?.click()}><Upload size={16}/>上传文件</button><input ref={fileRef} hidden type="file" onChange={event => { if (event.target.files?.[0]) void upload(event.target.files[0]) }}/><button className="secondary" disabled={!active} onClick={() => void load()}><Settings2 size={16}/>刷新</button></div>
    {error && <p className="panel-error" role="alert">{error}</p>}
    <div className="file-list">{path !== '.' && <button onClick={() => void load(path.split('/').slice(0, -1).join('/') || '.')}><Folder/><span>..</span></button>}{items.map(item => <button key={item.path} aria-pressed={item.type === 'file' ? item.path === selected : undefined} onClick={() => item.type === 'directory' ? void load(item.path) : setSelected(item.path)}><span className="file-icon">{item.type === 'directory' ? <Folder/> : <File/>}</span><span>{item.name}</span><small>{item.size !== undefined ? `${Math.ceil(item.size / 1024)} KB` : ''}</small>{item.type === 'directory' && <ChevronRight/>}</button>)}</div>
    {active ? <FileOperationsPanel conversationId={active.id} workspace={workspace} mode={mode} selected={selected} onChanged={() => void load()}/> : <p className="empty-panel">请先选择一个工作区对话。</p>}
  </section>
}
