import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Ban, Brain, Check, Pencil, Plus, Save, Trash2, X } from 'lucide-react'
import { api } from '../api'
import type { WorkspaceMemory } from '../types'

type MemoryFilter = 'all' | 'project' | 'experience'
interface MemoryDraft { key: string; content: string; kind: 'project' | 'experience'; tags: string; applicable_version: string }

const blankDraft: MemoryDraft = { key: '', content: '', kind: 'project', tags: '', applicable_version: '' }

export function MemoryManager({ workspace }: { workspace: string }) {
  const [items, setItems] = useState<WorkspaceMemory[]>([])
  const [filter, setFilter] = useState<MemoryFilter>('all')
  const [draft, setDraft] = useState(blankDraft)
  const [editing, setEditing] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState(blankDraft)
  const [error, setError] = useState('')

  const endpoint = `/api/memories?workspace=${encodeURIComponent(workspace)}`
  const load = useCallback(() => { api<WorkspaceMemory[]>(endpoint).then(setItems).catch(caught => setError((caught as Error).message)) }, [endpoint])
  useEffect(load, [load])
  const visible = useMemo(() => filter === 'all' ? items : items.filter(item => item.kind === filter), [filter, items])

  async function create(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await api(endpoint, {
        method: 'POST',
        body: JSON.stringify({
          key: draft.key,
          content: draft.content,
          kind: draft.kind,
          tags: draft.tags.split(',').map(item => item.trim()).filter(Boolean),
          applicable_version: draft.applicable_version || null,
        }),
      })
      setDraft(blankDraft)
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  function beginEdit(item: WorkspaceMemory) {
    setEditing(item.id)
    setEditDraft({ key: item.key, content: item.content, kind: item.kind, tags: item.tags.join(', '), applicable_version: item.applicable_version || '' })
  }

  async function saveEdit(item: WorkspaceMemory) {
    setError('')
    try {
      await api(`/api/memories/${item.id}?workspace=${encodeURIComponent(workspace)}`, {
        method: 'PATCH',
        body: JSON.stringify({
          key: editDraft.key,
          content: editDraft.content,
          kind: editDraft.kind,
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
    <header><span><Brain size={18} /></span><div><h3>上下文记忆</h3><p>项目事实与已验证经验会按任务相关度加载</p></div><div className="memory-tabs" role="tablist">{(['all', 'project', 'experience'] as const).map(value => <button className={filter === value ? 'active' : ''} key={value} onClick={() => setFilter(value)}>{value === 'all' ? '全部' : value === 'project' ? '项目' : '经验'}</button>)}</div></header>
    <div className="memory-layout">
      <form className="memory-form" onSubmit={create}>
        <label>记忆键<input value={draft.key} onChange={event => setDraft({ ...draft, key: event.target.value })} placeholder="build.command" required /></label>
        <label>内容<textarea value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })} placeholder="记录可复用且可验证的项目事实" required /></label>
        <div><label>类型<select value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value as 'project' | 'experience' })}><option value="project">项目记忆</option><option value="experience">经验记忆</option></select></label><label>适用版本<input value={draft.applicable_version} onChange={event => setDraft({ ...draft, applicable_version: event.target.value })} placeholder="可选" /></label></div>
        <label>标签<input value={draft.tags} onChange={event => setDraft({ ...draft, tags: event.target.value })} placeholder="逗号分隔" /></label>
        <button className="primary"><Plus size={15} />添加记忆</button>
        {error && <p className="panel-error">{error}</p>}
      </form>
      <div className="memory-list">{visible.length ? visible.map(item => <article className={`memory-item ${item.status}`} key={item.id}>
        {editing === item.id ? <>
          <div className="memory-edit-grid"><input value={editDraft.key} onChange={event => setEditDraft({ ...editDraft, key: event.target.value })} /><select value={editDraft.kind} onChange={event => setEditDraft({ ...editDraft, kind: event.target.value as 'project' | 'experience' })}><option value="project">项目</option><option value="experience">经验</option></select><textarea value={editDraft.content} onChange={event => setEditDraft({ ...editDraft, content: event.target.value })} /><input value={editDraft.tags} onChange={event => setEditDraft({ ...editDraft, tags: event.target.value })} placeholder="标签" /><input value={editDraft.applicable_version} onChange={event => setEditDraft({ ...editDraft, applicable_version: event.target.value })} placeholder="适用版本" /></div>
          <div className="memory-actions"><button title="保存" onClick={() => saveEdit(item)}><Save size={15} /></button><button title="取消" onClick={() => setEditing(null)}><X size={15} /></button></div>
        </> : <>
          <div className="memory-copy"><div><strong>{item.key}</strong><span>{item.kind === 'project' ? '项目' : '经验'} · 可信度 {Math.round(item.effective_confidence * 100)}%</span></div><p>{item.content}</p><small>{item.source} · 使用 {item.use_count} 次{item.applicable_version ? ` · ${item.applicable_version}` : ''}{item.stale_reasons.length ? ` · ${item.stale_reasons.join('、')}` : ''}</small></div>
          <div className="memory-actions"><button title="标记已验证" onClick={() => feedback(item, 'verify')}><Check size={15} /></button><button title="编辑" onClick={() => beginEdit(item)}><Pencil size={15} /></button><button title="否定并停用" onClick={() => feedback(item, 'reject')}><Ban size={15} /></button><button title="删除" onClick={() => remove(item)}><Trash2 size={15} /></button></div>
        </>}
      </article>) : <div className="empty-panel"><Brain /><p>当前分类还没有记忆</p></div>}</div>
    </div>
  </section>
}
