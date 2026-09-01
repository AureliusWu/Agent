import { useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { PermissionMode } from '../../types'
import { buildFileOperation, canConfirmFileOperation, deletionSummary, FILE_ACTION_LABELS, fileOperationError } from '../../shared/fileOperationPolicy'
import type { FileAction, FileOperation, FileToolResult } from '../../shared/fileOperationPolicy'
import { LatestRequest } from '../../shared/latestRequest'
import '../../styles/files.css'

interface FileChange { id: string; operation: string; created_at: string; entries?: { path: string }[] }
interface Props { conversationId: number; workspace: string; mode: PermissionMode; selected: string; onChanged: () => void }

export function FileOperationsPanel({ conversationId, workspace, mode, selected, onChanged }: Props) {
  const [action, setAction] = useState<FileAction>('create')
  const [source, setSource] = useState('')
  const [destination, setDestination] = useState('')
  const [content, setContent] = useState('')
  const [version, setVersion] = useState('')
  const [diff, setDiff] = useState('')
  const [plan, setPlan] = useState<FileOperation[]>([])
  const [preview, setPreview] = useState<FileToolResult | null>(null)
  const [result, setResult] = useState<FileToolResult | null>(null)
  const [changes, setChanges] = useState<FileChange[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const reader = useRef(new LatestRequest())
  const readonly = mode === 'readonly'
  const destructive = deletionSummary(plan)
  useEffect(() => { const gate = reader.current; mounted.current = true; return () => { mounted.current = false; gate.begin() } }, [])
  useEffect(() => {
    if (selected) { setSource(selected); setAction('edit'); setVersion(''); setContent(''); setDiff(''); reader.current.begin() }
  }, [selected])

  const execute = (tool: string, args: Record<string, unknown>, approvalTokens: string[] = []) => api<FileToolResult>('/api/tools/execute', {
    method: 'POST', body: JSON.stringify({ conversation_id: conversationId, workspace, permission_mode: mode, tool, arguments: args, approval_tokens: approvalTokens }),
  })

  async function loadChanges() {
    const response = await execute('list_file_changes', {})
    if (!mounted.current) return
    if (response.status !== 'ok') throw new Error(fileOperationError(response))
    setChanges(Array.isArray(response.changes) ? response.changes as FileChange[] : [])
  }
  // The containing panel is keyed by conversation/workspace/permission.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadChanges().catch(caught => { if (mounted.current) setError((caught as Error).message) }) }, [])

  async function run(work: () => Promise<void>) {
    if (busy) return
    setBusy(true); setError('')
    try { await work() } catch (caught) { if (mounted.current) setError((caught as Error).message) }
    finally { if (mounted.current) setBusy(false) }
  }

  async function authorized(tool: string, args: Record<string, unknown>, confirmation: string) {
    if (readonly) throw new Error('只读模式禁止修改，也不能通过确认解除')
    let response = await execute(tool, args)
    if (!mounted.current) return null
    if (canConfirmFileOperation(mode, response)) {
      if (!window.confirm(confirmation)) { setResult({ status: 'cancelled', message: '已拒绝授权，未继续执行' }); return null }
      response = await execute(tool, args, [response.approval_key!])
    }
    if (!mounted.current) return null
    const visibleResult = { ...response }
    delete visibleResult.approval_key
    setResult(visibleResult)
    if (response.status !== 'ok') throw new Error(fileOperationError(response))
    return response
  }

  function clearEditorVersion() { setVersion(''); setDiff(''); reader.current.begin() }

  async function readSelected() {
    const request = reader.current.begin()
    const response = await execute('read_file', { path: source })
    if (!mounted.current || !reader.current.isLatest(request)) return
    if (response.status !== 'ok') throw new Error(fileOperationError(response))
    if (response.truncated) throw new Error('文件内容超过编辑上限，不能使用截断内容覆盖原文件')
    setContent(String(response.content ?? '')); setVersion(String(response.version_token ?? '')); setDiff('')
  }

  async function stage() {
    if (readonly) throw new Error('只读模式禁止修改')
    if (plan.length >= 50) throw new Error('单批最多 50 项，请先执行或清空计划')
    let sourceVersion = version
    if (action !== 'create' && action !== 'edit') {
      const metadata = await execute('file_metadata', { path: source })
      if (!mounted.current) return
      if (metadata.status !== 'ok') throw new Error(fileOperationError(metadata))
      if (metadata.type !== 'file') throw new Error('此操作区仅处理单个文件；目录请在文件列表中浏览')
      sourceVersion = String(metadata.version_token ?? '')
    }
    const operation = buildFileOperation(action, source, destination, content, sourceVersion)
    const touched = (item: FileOperation) => [item.arguments.path, item.arguments.source, item.arguments.destination].filter(Boolean).map(path => String(path).toLowerCase())
    if (plan.some(item => touched(item).some(path => touched(operation).includes(path)))) throw new Error('同一文件不能在一个批次重复修改，请先执行当前批次')
    if (action === 'edit' || action === 'create') {
      const response = await execute('file_diff', { path: source, content })
      if (!mounted.current) return
      if (response.status !== 'ok') throw new Error(fileOperationError(response))
      setDiff(String(response.diff || '没有文本差异'))
    }
    setPlan(old => [...old, operation]); setPreview(null); setResult(null)
  }

  async function runBatch(dryRun: boolean) {
    if (!plan.length || (!dryRun && !preview)) return
    const summary = destructive || `本批次包含 ${plan.length} 项文件操作，成功修改会创建可撤销记录。`
    if (!dryRun && !window.confirm(`执行已预览的 ${plan.length} 项操作？\n${summary}`)) return
    setPreview(null)
    const response = await authorized('file_batch', { operations: plan, dry_run: dryRun }, `${dryRun ? '预检' : '执行'}当前批次？\n${summary}`)
    if (!response) return
    if (dryRun) setPreview(response)
    else { setPlan([]); setPreview(null); setVersion(''); await loadChanges(); if (mounted.current) onChanged() }
  }

  async function undo(change: FileChange) {
    const targets = change.entries?.map(entry => entry.path).join('、') || change.id
    if (!window.confirm(`撤销 ${change.operation}？\n目标：${targets}\n数量：${change.entries?.length ?? '待核心校验'} 项\n恢复：依赖原备份；当前文件有冲突时停止，不强制覆盖。`)) return
    const response = await authorized('undo_file_change', { change_id: change.id }, `授权撤销变更 ${change.id}？\n目标：${targets}`)
    if (response) { setPreview(null); setVersion(''); await loadChanges(); if (mounted.current) onChanged() }
  }

  return <section className="file-operations" aria-label="文件操作">
    <header><h3>文件操作</h3><span>{readonly ? '只读 · 写入和撤销均被禁用' : '版本校验 · 不覆盖已有目标 · 可撤销备份'}</span></header>
    <div className="file-operation-form">
      <label>操作<select value={action} disabled={busy} onChange={event => { setAction(event.target.value as FileAction); clearEditorVersion() }}>{Object.entries(FILE_ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>文件路径<input value={source} disabled={busy} placeholder="例如 docs/note.md" onChange={event => { setSource(event.target.value); clearEditorVersion() }}/></label>
      {['rename', 'move', 'copy'].includes(action) && <label>目标路径<input value={destination} disabled={busy} placeholder="目标必须尚不存在" onChange={event => setDestination(event.target.value)}/></label>}
      {(action === 'create' || action === 'edit') && <label className="file-content-label">文本内容<textarea value={content} disabled={busy} onChange={event => setContent(event.target.value)} rows={6}/></label>}
    </div>
    <div className="panel-toolbar">{action === 'edit' && <button className="secondary" disabled={busy || !source} onClick={() => void run(readSelected)}>读取文件版本</button>}<button className="secondary" disabled={busy || readonly || !source} onClick={() => void run(stage)}>加入计划并预览差异</button></div>
    {diff && <pre className="file-diff" aria-label="文本差异">{diff}</pre>}
    {plan.length > 0 && <div className="file-plan"><h4>待执行计划 · {plan.length} 项</h4><ol>{plan.map((item, index) => <li key={index}><code>{item.operation}</code><span>{String(item.arguments.path || item.arguments.source)}{item.arguments.destination ? ` → ${item.arguments.destination}` : ''}</span><button className="secondary" disabled={busy} onClick={() => { setPlan(old => old.filter((_, i) => i !== index)); setPreview(null) }}>移除</button></li>)}</ol>{destructive && <p className="file-delete-summary">{destructive}</p>}<p>{preview ? '核心预检通过；执行时仍会再次检查文件版本。' : '计划尚未执行，请先预检。'}</p><div className="panel-toolbar"><button className="secondary" disabled={busy || readonly} onClick={() => void run(() => runBatch(true))}>预检批次</button><button className="primary" disabled={busy || readonly || !preview} onClick={() => void run(() => runBatch(false))}>执行 {plan.length} 项</button></div></div>}
    {error && <p className="panel-error" role="alert">{error}</p>}
    {result && <details className="file-operation-result" open><summary>操作结果 · {String(result.status || 'unknown')}</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
    <details className="file-undo-timeline"><summary>撤销时间线 · {changes.length} 条</summary><button className="secondary" disabled={busy} onClick={() => void run(loadChanges)}>刷新记录</button>{changes.length === 0 && <p>当前工作区没有可撤销记录。</p>}<ol>{changes.map(change => <li key={change.id}><div><strong>{change.operation}</strong><time>{change.created_at}</time><span>{change.entries?.map(entry => entry.path).join('、')}</span></div><button className="secondary" disabled={busy || readonly} onClick={() => void run(() => undo(change))}>撤销此项</button></li>)}</ol></details>
  </section>
}
