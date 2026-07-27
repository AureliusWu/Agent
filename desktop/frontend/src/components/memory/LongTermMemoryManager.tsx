import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Brain, Check, EyeOff, Lock, Pencil, Plus, Save, Search, ShieldCheck, Trash2, Unlock, X } from 'lucide-react'
import { api } from '../../api'
import { adminUiSessionId, issueAdminActionGrant } from '../../adminActionGrants'
import { LatestRequest } from '../../shared/latestRequest'
import { HighlightedText } from '../shared/HighlightedText'
import type { LongTermMemory, LongTermMemorySearchItem, LongTermMemorySearchResponse, LongTermMemoryType } from '../../types'

const typeLabels: Record<LongTermMemoryType, string> = {
  semantic: '语义记忆',
  episodic: '情景记忆',
  procedural: '程序记忆',
  relationship: '关系记忆',
}
const types = Object.keys(typeLabels) as LongTermMemoryType[]
const emptyDraft = { title: '', content: '', memory_type: 'semantic' as LongTermMemoryType, importance: 0.6, is_locked: false, is_sensitive: false }
interface MemoryCandidate { id: string; memory_type: LongTermMemoryType; content: string; reason: string; confidence: number; importance: number }
const isSearchItem = (item: LongTermMemory | LongTermMemorySearchItem): item is LongTermMemorySearchItem => 'ranking' in item

export function LongTermMemoryManager({ initialQuery = '', focusMemoryId = '' }: { initialQuery?: string; focusMemoryId?: string }) {
  const [uiSessionId] = useState(adminUiSessionId)
  const [items, setItems] = useState<LongTermMemory[]>([])
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([])
  const [draft, setDraft] = useState(emptyDraft)
  const [filter, setFilter] = useState<'all' | LongTermMemoryType>('all')
  const [statusFilter, setStatusFilter] = useState('active')
  const [sort, setSort] = useState<'relevance' | 'updated' | 'importance'>('relevance')
  const [sensitiveMode, setSensitiveMode] = useState<'exclude' | 'redacted' | 'full'>('exclude')
  const [query, setQuery] = useState(initialQuery)
  const [searchResult, setSearchResult] = useState<LongTermMemorySearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const searchSequence = useRef(new LatestRequest())
  const [editing, setEditing] = useState<string | null>(null)
  const [editContent, setEditContent] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(() => {
    void Promise.all([
      api<LongTermMemory[]>(`/api/long-term-memories?status=${encodeURIComponent(statusFilter)}&include_sensitive=false`),
      api<MemoryCandidate[]>('/api/long-term-memories/candidates'),
    ]).then(([memories, pending]) => { setItems(memories); setCandidates(pending) }).catch(caught => setError((caught as Error).message))
  }, [statusFilter])
  useEffect(() => { load() }, [load])
  useEffect(() => { setQuery(initialQuery) }, [initialQuery])
  useEffect(() => {
    const normalized = query.trim()
    const sequence = searchSequence.current.begin()
    if (!normalized) {
      setSearchResult(null)
      setSearching(false)
      return
    }
    setSearching(true)
    const timer = window.setTimeout(() => {
      const request = {
          query: normalized,
          memory_types: filter === 'all' ? [] : [filter],
          statuses: [statusFilter],
          sensitive_mode: sensitiveMode,
          sort,
          offset: 0,
          limit: 50,
      }
      const search = async () => {
        const adminGrantToken = sensitiveMode === 'exclude'
          ? undefined
          : await issueAdminActionGrant('memory.search_sensitive', 'search', request, uiSessionId)
        return api<LongTermMemorySearchResponse>('/api/long-term-memories/search', {
          method: 'POST',
          body: JSON.stringify({ ...request, admin_grant_token: adminGrantToken, ui_session_id: adminGrantToken ? uiSessionId : undefined }),
        })
      }
      void search().then(result => {
        if (searchSequence.current.isLatest(sequence)) setSearchResult(result)
      }).catch(caught => {
        if (searchSequence.current.isLatest(sequence)) setError((caught as Error).message)
      }).finally(() => {
        if (searchSequence.current.isLatest(sequence)) setSearching(false)
      })
    }, 180)
    return () => window.clearTimeout(timer)
  }, [filter, query, sensitiveMode, sort, statusFilter, uiSessionId])
  const visible = useMemo(() => filter === 'all' ? items : items.filter(item => item.memory_type === filter), [filter, items])
  const displayed = searchResult ? searchResult.items : visible

  useEffect(() => {
    if (!focusMemoryId || !displayed.some(item => item.id === focusMemoryId)) return
    window.requestAnimationFrame(() => focusMemory(focusMemoryId))
  }, [displayed, focusMemoryId])

  function focusMemory(id: string) {
    document.querySelector(`[data-memory-id="${CSS.escape(id)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  function changeSensitiveMode(value: 'exclude' | 'redacted' | 'full') {
    if (value !== 'exclude' && !confirm(value === 'full' ? '确认以管理员身份显示搜索命中的完整敏感记忆？' : '确认以管理员身份搜索并显示去敏后的敏感记忆？')) return
    setSensitiveMode(value)
  }

  function clearSearch() {
    setQuery('')
    setFilter('all')
    setStatusFilter('active')
    setSort('relevance')
    setSensitiveMode('exclude')
    setError('')
  }

  async function create(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      const values = {
        ...draft,
        title: draft.title || null,
        source_type: 'manual_entry',
        confidence: 1,
        user_confirmed: true,
      }
      const adminGrantToken = await issueAdminActionGrant('memory.create', 'new', values, uiSessionId)
      await api('/api/long-term-memories', {
        method: 'POST',
        body: JSON.stringify({ ...values, admin_grant_token: adminGrantToken, ui_session_id: uiSessionId }),
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
      const adminGrantToken = await issueAdminActionGrant('memory.update', item.id, changes, uiSessionId)
      await api(`/api/long-term-memories/${item.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ ...changes, admin_grant_token: adminGrantToken, ui_session_id: uiSessionId }),
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
      const adminGrantToken = await issueAdminActionGrant('memory.delete', item.id, {}, uiSessionId)
      const query = new URLSearchParams({ admin_grant_token: adminGrantToken, ui_session_id: uiSessionId })
      await api(`/api/long-term-memories/${item.id}?${query}`, { method: 'DELETE' })
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  async function decide(candidate: MemoryCandidate, accept: boolean) {
    try {
      const adminGrantToken = accept
        ? await issueAdminActionGrant('memory_candidate.accept', candidate.id, { accept: true }, uiSessionId)
        : undefined
      await api(`/api/long-term-memories/candidates/${candidate.id}/decision`, {
        method: 'POST',
        body: JSON.stringify({ accept, admin_grant_token: adminGrantToken, ui_session_id: accept ? uiSessionId : undefined }),
      })
      load()
    } catch (caught) { setError((caught as Error).message) }
  }

  return <section className="long-memory-manager">
    <header className="long-memory-toolbar">
      <div><Brain size={18} /><span><strong>长期记忆</strong><small>身份核心之外、可追溯且可管理的连续记忆</small></span></div>
      <label>类型<select value={filter} onChange={event => setFilter(event.target.value as 'all' | LongTermMemoryType)}><option value="all">全部</option>{types.map(type => <option key={type} value={type}>{typeLabels[type]}</option>)}</select></label>
      <label>状态<select value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="active">有效</option><option value="archived">已归档</option><option value="superseded">已替代</option><option value="expired">已过期</option></select></label>
      <label>排序<select value={sort} onChange={event => setSort(event.target.value as typeof sort)}><option value="relevance">相关度</option><option value="updated">更新时间</option><option value="importance">重要性</option></select></label>
      <label>敏感内容<select value={sensitiveMode} onChange={event => changeSensitiveMode(event.target.value as typeof sensitiveMode)}><option value="exclude">默认隐藏</option><option value="redacted">授权后去敏</option><option value="full">授权后完整</option></select></label>
      <button type="button" onClick={clearSearch}>清除条件</button>
    </header>
    <label className="long-memory-search"><Search size={17} /><span className="sr-only">搜索长期记忆</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题、正文或标签" />{searching && <small>搜索中…</small>}</label>
    {searchResult && <p className="search-note">找到 {searchResult.page.total} 条个人长期记忆 · 排名依据可展开查看</p>}
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
      <div className="memory-list">{displayed.length ? displayed.map(item => <article className="memory-item" data-memory-id={item.id} key={item.id} onClick={() => focusMemory(item.id)}>
        <div className="memory-copy">
          <div><strong>{isSearchItem(item) ? <HighlightedText text={item.title || typeLabels[item.memory_type]} terms={item.matched_terms} /> : item.title || typeLabels[item.memory_type]}</strong><span>{typeLabels[item.memory_type]} · 可信度 {Math.round(item.confidence * 100)}%</span></div>
          {editing === item.id ? <textarea value={editContent} onChange={event => setEditContent(event.target.value)} /> : <p>{isSearchItem(item) ? <HighlightedText text={item.content} terms={item.matched_terms} /> : item.content}</p>}
          <small>{item.source_type}{item.user_confirmed ? ' · 管理员确认' : ''}{item.is_locked ? ' · 已锁定' : ''}{item.is_sensitive ? ' · 敏感' : ''}</small>
          {isSearchItem(item) && <details><summary>为何命中 · {item.matched_fields.join('、') || '全文索引'}</summary><small>相关度 {item.score.toFixed(3)} · 重要性 {item.importance.toFixed(2)} · 可信度 {item.confidence.toFixed(2)}</small></details>}
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
