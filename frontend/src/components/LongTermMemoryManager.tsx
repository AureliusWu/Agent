import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Brain, Check, EyeOff, Lock, Pencil, Plus, Save, ShieldCheck, Trash2, Unlock, X } from 'lucide-react'
import { api } from '../api'
import type { LongTermMemory, LongTermMemoryType } from '../types'

const typeLabels: Record<LongTermMemoryType, string> = {
  semantic: '语义记忆',
  episodic: '情景记忆',
  procedural: '程序记忆',
  relationship: '关系记忆',
}
const types = Object.keys(typeLabels) as LongTermMemoryType[]
const emptyDraft = { title: '', content: '', memory_type: 'semantic' as LongTermMemoryType, importance: 0.6, is_locked: false, is_sensitive: false }
interface MemoryCandidate { id: string; memory_type: LongTermMemoryType; content: string; reason: string; confidence: number; importance: number }

export function LongTermMemoryManager() {
  const [items, setItems] = useState<LongTermMemory[]>([])
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([])
  const [draft, setDraft] = useState(emptyDraft)
  const [filter, setFilter] = useState<'all' | LongTermMemoryType>('all')
  const [editing, setEditing] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => {
    void Promise.all([
      api<LongTermMemory[]>('/api/long-term-memories'),
      api<MemoryCandidate[]>('/api/long-term-memories/candidates'),
    ]).then(([memories, pending]) => { setItems(memories); setCandidates(pending) }).catch(caught => setError((caught as Error).message))
  }, [])
  useEffect(() => { load() }, [load])
  const visible = useMemo(() => filter === 'all' ? items : items.filter(item => item.memory_type === filter), [filter, items])

  async function create(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await api('/api/long-term-memories', {
        method: 'POST',
        body: JSON.stringify({
          ...draft,
          title: draft.title || null,
          source_type: 'manual_entry',
          confidence: 1,
          user_confirmed: true,
        }),
      })
      setDraft(emptyDraft)
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function update(item: LongTermMemory, changes: Record<string, unknown>) {
    const lockedChange = item.is_locked || changes.is_locked === true
    const administratorConfirmed = !lockedChange || confirm('这是锁定记忆。确认以管理员身份修改吗？')
    if (!administratorConfirmed) return
    try {
      await api(`/api/long-term-memories/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...changes, administrator_confirmed: administratorConfirmed }),
      })
      setEditing(null)
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function remove(item: LongTermMemory) {
    if (!confirm(`遗忘“${item.title || item.content.slice(0, 24)}”？该记录会保留删除标记，不再自动召回。`)) return
    const confirmed = !item.is_locked || confirm('这是一条锁定记忆，需要再次确认删除。')
    if (!confirmed) return
    try {
      await api(`/api/long-term-memories/${item.id}?administrator_confirmed=${confirmed}`, { method: 'DELETE' })
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function decide(candidate: MemoryCandidate, accept: boolean) {
    try {
      await api(`/api/long-term-memories/candidates/${candidate.id}/decision`, {
        method: 'POST',
        body: JSON.stringify({ accept, administrator_confirmed: accept }),
      })
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  return <section className="long-memory-manager">
    <header className="long-memory-toolbar">
      <div><Brain size={18} /><span><strong>长期记忆</strong><small>身份核心之外、可追溯且可管理的连续记忆</small></span></div>
      <label>类型<select value={filter} onChange={event => setFilter(event.target.value as 'all' | LongTermMemoryType)}><option value="all">全部</option>{types.map(type => <option key={type} value={type}>{typeLabels[type]}</option>)}</select></label>
    </header>
    {candidates.length > 0 && <section className="memory-candidates"><h4>待确认候选 <span>{candidates.length}</span></h4>{candidates.map(candidate => <article key={candidate.id}><div><strong>{typeLabels[candidate.memory_type]}</strong><p>{candidate.content}</p><small>{candidate.reason} · 可信度 {Math.round(candidate.confidence * 100)}%</small></div><div className="memory-actions"><button title="确认写入" onClick={() => decide(candidate, true)}><Check size={15} /></button><button title="拒绝" onClick={() => decide(candidate, false)}><X size={15} /></button></div></article>)}</section>}
    <div className="memory-layout">
      <form className="memory-form" onSubmit={create}>
        <label>标题<input value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} placeholder="可选" /></label>
        <label>内容<textarea required value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })} placeholder="写下希望夏目心长期记住的事实、事件或协作方式" /></label>
        <div><label>类型<select value={draft.memory_type} onChange={event => setDraft({ ...draft, memory_type: event.target.value as LongTermMemoryType })}>{types.map(type => <option key={type} value={type}>{typeLabels[type]}</option>)}</select></label><label>重要性<input type="number" min="0" max="1" step="0.1" value={draft.importance} onChange={event => setDraft({ ...draft, importance: Number(event.target.value) })} /></label></div>
        <div className="memory-checks"><label><input type="checkbox" checked={draft.is_locked} onChange={event => setDraft({ ...draft, is_locked: event.target.checked })} />锁定</label><label><input type="checkbox" checked={draft.is_sensitive} onChange={event => setDraft({ ...draft, is_sensitive: event.target.checked })} />敏感</label></div>
        <button className="primary"><Plus size={15} />添加长期记忆</button>
        {error && <p className="panel-error">{error}</p>}
      </form>
      <div className="memory-list">{visible.length ? visible.map(item => <article className="memory-item" key={item.id}>
        <div className="memory-copy">
          <div><strong>{item.title || typeLabels[item.memory_type]}</strong><span>{typeLabels[item.memory_type]} · 可信度 {Math.round(item.confidence * 100)}%</span></div>
          {editing === item.id ? <textarea value={editContent} onChange={event => setEditContent(event.target.value)} /> : <p>{item.content}</p>}
          <small>{item.source_type}{item.user_confirmed ? ' · 管理员确认' : ''}{item.is_locked ? ' · 已锁定' : ''}{item.is_sensitive ? ' · 敏感' : ''}</small>
        </div>
        <div className="memory-actions">
          {item.user_confirmed && <ShieldCheck size={15} aria-label="管理员确认" />}
          {item.is_sensitive && <EyeOff size={15} aria-label="敏感记忆" />}
          {editing === item.id ? <><button title="保存" onClick={() => update(item, { content: editContent })}><Save size={15} /></button><button title="取消" onClick={() => setEditing(null)}><X size={15} /></button></> : <button title="编辑" onClick={() => { setEditing(item.id); setEditContent(item.content) }}><Pencil size={15} /></button>}
          <button title={item.is_locked ? '解锁' : '锁定'} onClick={() => update(item, { is_locked: !item.is_locked })}>{item.is_locked ? <Unlock size={15} /> : <Lock size={15} />}</button>
          <button title="遗忘" onClick={() => remove(item)}><Trash2 size={15} /></button>
        </div>
      </article>) : <div className="empty-panel"><Brain /><p>还没有符合条件的长期记忆</p></div>}</div>
    </div>
  </section>
}
