import { useEffect, useRef, useState } from 'react'
import { ChevronRight, File, Files, Folder, Settings2, Upload } from 'lucide-react'
import { api, apiFetch } from '../../api'
import type { Conversation, FileItem, PermissionMode } from '../../types'
import { PanelHeader } from '../shared/PanelHeader'
import { FileOperationsPanel } from './FileOperationsPanel'
import { LatestRequest } from '../../shared/latestRequest'
import { canConfirmFileOperation, fileOperationError } from '../../shared/fileOperationPolicy'
import type { FileToolResult } from '../../shared/fileOperationPolicy'
import { filterFilePaths, MAX_BULK_FILES } from '../../shared/bulkFilePlan'
import '../../styles/panels.css'

interface Props { active: Conversation | null; workspace: string; mode: PermissionMode }
export function FilesPanel(props: Props) {
  return <ScopedFilesPanel key={`${props.active?.id ?? 'none'}:${props.workspace}:${props.mode}`} {...props}/>
}

function ScopedFilesPanel({ active, workspace, mode }: Props) {
  const [items, setItems] = useState<FileItem[]>([])
  const [path, setPath] = useState('.')
  const [selected, setSelected] = useState('')
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const mounted = useRef(true)
  const uploadLock = useRef(false)
  const requests = useRef(new LatestRequest())
  useEffect(() => { const gate = requests.current; mounted.current = true; return () => { mounted.current = false; gate.begin() } }, [])

  async function load(next = path) {
    if (!active) return
    const request = requests.current.begin()
    try {
      const result = await api<FileToolResult & { items?: FileItem[] }>('/api/tools/execute', { method: 'POST', body: JSON.stringify({ conversation_id: active.id, workspace, permission_mode: mode, tool: 'list_files', arguments: { path: next } }) })
      if (!mounted.current || !requests.current.isLatest(request)) return
      if (result.status !== 'ok') throw new Error(fileOperationError(result))
      setItems((result.items || []).map(item => ({ ...item, path: item.path.replaceAll('\\', '/') }))); setPath(next.replaceAll('\\', '/')); setError('')
    } catch (caught) { if (mounted.current && requests.current.isLatest(request)) setError((caught as Error).message) }
  }
  // The parent key remounts this scope for every conversation, workspace, or permission change.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void load('.') }, [])

  async function upload(file: globalThis.File) {
    if (!active || mode === 'readonly' || uploadLock.current || !mounted.current) return
    uploadLock.current = true
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
    finally { uploadLock.current = false; if (mounted.current) { setUploading(false); if (fileRef.current) fileRef.current.value = '' } }
  }

  const visiblePaths = new Set(filterFilePaths(items.map(item => item.path), filter))
  const visibleItems = items.filter(item => visiblePaths.has(item.path))
  function toggle(path: string) {
    if (selectedFiles.includes(path)) { setSelectedFiles(old => old.filter(item => item !== path)); return }
    if (selectedFiles.length >= MAX_BULK_FILES) { setError('最多选择 50 个文件，请先移除部分已选项'); return }
    setSelectedFiles(old => old.includes(path) ? old : old.length < MAX_BULK_FILES ? [...old, path] : old)
  }
  function selectVisible() {
    const next = [...new Set([...selectedFiles, ...visibleItems.filter(item => item.type === 'file').map(item => item.path)])]
    if (next.length > MAX_BULK_FILES) { setError('筛选结果超过 50 个文件，请缩小范围后再选择'); return }
    setSelectedFiles(next); setError('')
  }

  return <section className="content-panel"><PanelHeader icon={<Files/>} title="工作区文件" subtitle={workspace}/>
    <div className="panel-toolbar"><button className="primary" disabled={!active || mode === 'readonly' || uploading} onClick={() => fileRef.current?.click()}><Upload size={16}/>上传文件</button><input ref={fileRef} hidden type="file" onChange={event => { if (event.target.files?.[0]) void upload(event.target.files[0]) }}/><button className="secondary" disabled={!active} onClick={() => void load()}><Settings2 size={16}/>刷新</button></div>
    {error && <p className="panel-error" role="alert">{error}</p>}
    {active && <div className="file-selection-toolbar"><label>筛选当前目录<input value={filter} placeholder="文件名或扩展名，例如 .txt" onChange={event => setFilter(event.target.value)}/></label><span role="status">已选 {selectedFiles.length} / 50</span><button className="secondary" onClick={selectVisible}>选择筛选结果</button><button className="secondary" disabled={!selectedFiles.length} onClick={() => setSelectedFiles([])}>清空选择</button></div>}
    <div className="file-list">{path !== '.' && <button onClick={() => void load(path.split('/').slice(0, -1).join('/') || '.')}><Folder/><span>..</span></button>}{visibleItems.map(item => <div className="file-selectable-row" key={item.path}>{item.type === 'file' && <input type="checkbox" aria-label={`选择 ${item.path}`} checked={selectedFiles.includes(item.path)} disabled={selectedFiles.length >= MAX_BULK_FILES && !selectedFiles.includes(item.path)} onChange={() => toggle(item.path)}/>}<button aria-pressed={item.type === 'file' ? item.path === selected : undefined} onClick={() => item.type === 'directory' ? void load(item.path) : setSelected(item.path)}><span className="file-icon">{item.type === 'directory' ? <Folder/> : <File/>}</span><span>{item.name}</span><small>{item.size !== undefined ? `${Math.ceil(item.size / 1024)} KB` : ''}</small>{item.type === 'directory' && <ChevronRight/>}</button></div>)}</div>
    {selectedFiles.length > 0 && <details className="file-selected-paths"><summary>查看已选文件（含其他目录）</summary><ul>{selectedFiles.map(item => <li key={item}><span>{item}</span><button className="secondary" onClick={() => toggle(item)}>取消选择</button></li>)}</ul></details>}
    {active ? <FileOperationsPanel conversationId={active.id} workspace={workspace} mode={mode} selected={selected} selectedFiles={selectedFiles} onChanged={() => { setSelectedFiles([]); void load() }}/> : <p className="empty-panel">请先选择一个工作区对话。</p>}
  </section>
}
