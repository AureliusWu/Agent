import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Ban, Brain, Check, Download, Pencil, Plus, Save, Trash2, Upload, X } from 'lucide-react'
import { api, apiFetch } from '../api'
import type { MemoryCategory, WorkspaceMemory } from '../types'

type MemoryFilter = 'all' | MemoryCategory
interface MemoryDraft { key: string; content: string; category: MemoryCategory; tags: string; applicable_version: string }

const categoryLabels: Record<MemoryCategory, string> = {
  architecture: '项目架构', build_command: '构建命令', test_command: '测试命令', coding_convention: '编码规范', decision: '技术决策', known_issue: '已知问题', successful_fix: '成功修复', failed_approach: '失败路径', user_constraint: '用户约束',
}
const categories = Object.keys(categoryLabels) as MemoryCategory[]
const blankDraft: MemoryDraft = { key: '', content: '', category: 'decision', tags: '', applicable_version: '' }

export function MemoryManager({ workspace }: { workspace: string }) {
  const [items, setItems] = useState<WorkspaceMemory[]>([])
  const [filter, setFilter] = useState<MemoryFilter>('all')
  const [draft, setDraft] = useState(blankDraft)
  const [editing, setEditing] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState(blankDraft)
  const [error, setError] = useState('')
  const [namespace, setNamespace] = useState<'personal' | 'project'>('personal')
  const [importing, setImporting] = useState(false)

  const endpoint = `/api/memories?workspace=${encodeURIComponent(workspace)}&namespace=${namespace}`
  const load = useCallback(() => { api<WorkspaceMemory[]>(endpoint).then(setItems).catch(caught => setError((caught as Error).message)) }, [endpoint])
  useEffect(load, [load])
  const visible = useMemo(() => filter === 'all' ? items : items.filter(item => item.category === filter), [filter, items])

  async function create(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await api(endpoint, {
        method: 'POST',
        body: JSON.stringify({
          key: draft.key,
          content: draft.content,
          namespace,
          category: draft.category,
          tags: draft.tags.split(',').map(item => item.trim()).filter(Boolean),
          applicable_version: draft.applicable_version || null,
        }),
      })
      setDraft(blankDraft)
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function importFile(file: File) {
    setImporting(true)
    setError('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('workspace', workspace)
      form.append('namespace', namespace)
      const response = await apiFetch('/api/memories/import', { method: 'POST', body: form })
      const result = await response.json() as { imported?: number; detail?: string }
      if (!response.ok) throw new Error(result.detail || '导入失败')
      load()
    } catch (caught) { setError((caught as Error).message) }
    finally { setImporting(false) }
  }

  async function exportFile() {
    try {
      const response = await apiFetch(`/api/memories/export?workspace=${encodeURIComponent(workspace)}&namespace=${namespace}`)
      if (!response.ok) throw new Error('导出失败')
      const blob = await response.blob()
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `siyi-${namespace}-memories.json`
      link.click()
      URL.revokeObjectURL(link.href)
    } catch (caught) { setError((caught as Error).message) }
  }

  function beginEdit(item: WorkspaceMemory) {
    setEditing(item.id)
    setEditDraft({ key: item.key, content: item.content, category: item.category, tags: item.tags.join(', '), applicable_version: item.applicable_version || '' })
  }

  async function saveEdit(item: WorkspaceMemory) {
    setError('')
    try {
      await api(`/api/memories/${item.id}?workspace=${encodeURIComponent(workspace)}`, {
        method: 'PATCH',
        body: JSON.stringify({
          key: editDraft.key,
          content: editDraft.content,
          category: editDraft.category,
          tags: editDraft.tags.split(',').map(value => value.trim()).filter(Boolean),
          applicable_version: editDraft.applicable_version || null,
        }),
      })
      setEditing(null)
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function remove(item: WorkspaceMemory) {
    if (!confirm(`删除记忆“${item.key}”？`)) return
    await api(`/api/memories/${item.id}?workspace=${encodeURIComponent(workspace)}`, { method: 'DELETE' })
    load()
  }

  async function feedback(item: WorkspaceMemory, outcome: 'verify' | 'reject') {
    await api(`/api/memories/${item.id}/feedback?workspace=${encodeURIComponent(workspace)}`, { method: 'POST', body: JSON.stringify({ outcome }) })
    load()
  }

  return <section className="memory-manager">
    <header><span><Brain size={18} /></span><div><h3>{namespace === 'personal' ? '我的记忆' : '项目记忆'}</h3><p>{namespace === 'personal' ? '由你明确编写或导入，不依赖工作区' : '项目事实、约束与验证经验'}</p></div><label className="memory-filter">分类<select value={filter} onChange={event => setFilter(event.target.value as MemoryFilter)}><option value="all">全部分类</option>{categories.map(value => <option key={value} value={value}>{categoryLabels[value]}</option>)}</select></label></header>
    <div className="memory-tabs">
      <button className={namespace === 'personal' ? 'active' : ''} onClick={() => setNamespace('personal')}>我的记忆</button>
      <button className={namespace === 'project' ? 'active' : ''} disabled={!workspace} title={!workspace ? '请先选择项目' : ''} onClick={() => setNamespace('project')}>项目记忆</button>
      <label className="memory-import"><Upload size={15} />{importing ? '导入中…' : '导入'}<input type="file" accept=".zip,.json,.md,.txt" hidden disabled={importing} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFile(file) }} /></label>
      <button onClick={exportFile}><Download size={15} />导出</button>
    </div>
    <div className="memory-layout">
      <form className="memory-form" onSubmit={create}>
        <label>记忆键<input value={draft.key} onChange={event => setDraft({ ...draft, key: event.target.value })} placeholder="build.command" required /></label>
        <label>内容<textarea value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })} placeholder="记录可复用且可验证的项目事实" required /></label>
        <div><label>分类<select value={draft.category} onChange={event => setDraft({ ...draft, category: event.target.value as MemoryCategory })}>{categories.map(value => <option key={value} value={value}>{categoryLabels[value]}</option>)}</select></label><label>适用版本<input value={draft.applicable_version} onChange={event => setDraft({ ...draft, applicable_version: event.target.value })} placeholder="可选" /></label></div>
        <label>标签<input value={draft.tags} onChange={event => setDraft({ ...draft, tags: event.target.value })} placeholder="逗号分隔" /></label>
        <button className="primary"><Plus size={15} />添加记忆</button>
        {error && <p className="panel-error">{error}</p>}
      </form>
      <div className="memory-list">{visible.length ? visible.map(item => <article className={`memory-item ${item.status}`} key={item.id}>
        {editing === item.id ? <>
          <div className="memory-edit-grid"><input value={editDraft.key} onChange={event => setEditDraft({ ...editDraft, key: event.target.value })} /><select value={editDraft.category} onChange={event => setEditDraft({ ...editDraft, category: event.target.value as MemoryCategory })}>{categories.map(value => <option key={value} value={value}>{categoryLabels[value]}</option>)}</select><textarea value={editDraft.content} onChange={event => setEditDraft({ ...editDraft, content: event.target.value })} /><input value={editDraft.tags} onChange={event => setEditDraft({ ...editDraft, tags: event.target.value })} placeholder="标签" /><input value={editDraft.applicable_version} onChange={event => setEditDraft({ ...editDraft, applicable_version: event.target.value })} placeholder="适用版本" /></div>
          <div className="memory-actions"><button title="保存" onClick={() => saveEdit(item)}><Save size={15} /></button><button title="取消" onClick={() => setEditing(null)}><X size={15} /></button></div>
        </> : <>
          <div className="memory-copy"><div><strong>{item.key}</strong><span>{categoryLabels[item.category]} · 可信度 {Math.round(item.effective_confidence * 100)}%</span></div><p>{item.content}</p><small>{item.source} · 使用 {item.use_count} 次{item.applicable_version ? ` · ${item.applicable_version}` : ''}{item.stale_reasons.length ? ` · ${item.stale_reasons.join('、')}` : ''}</small></div>
          <div className="memory-actions"><button title="标记已验证" onClick={() => feedback(item, 'verify')}><Check size={15} /></button><button title="编辑" onClick={() => beginEdit(item)}><Pencil size={15} /></button><button title="否定并停用" onClick={() => feedback(item, 'reject')}><Ban size={15} /></button><button title="删除" onClick={() => remove(item)}><Trash2 size={15} /></button></div>
        </>}
      </article>) : <div className="empty-panel"><Brain /><p>当前分类还没有记忆</p></div>}</div>
    </div>
  </section>
}
