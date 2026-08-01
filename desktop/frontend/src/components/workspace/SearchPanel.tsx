import { Brain, Folder, MessageSquare, Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api'
import { groupGlobalMemoryResults } from '../../shared/globalMemorySearch'
import { LatestRequest } from '../../shared/latestRequest'
import { HighlightedText } from '../shared/HighlightedText'
import type { Conversation, GlobalMemorySearchItem, GlobalMemorySearchResponse, LongTermMemorySearchItem, LongTermMemorySearchResponse, View } from '../../types'

interface Props {
  conversations: Conversation[]
  workspace: string
  onSelectConversation: (item: Conversation) => void
  onNavigate: (view: View) => void
  onOpenLongTermMemory: (query: string, memoryId: string) => void
  initialQuery?: string
}

export function SearchPanel({ conversations, workspace, onSelectConversation, onNavigate, onOpenLongTermMemory, initialQuery = '' }: Props) {
  const [query, setQuery] = useState(initialQuery)
  const [globalMemoryItems, setGlobalMemoryItems] = useState<GlobalMemorySearchItem[]>([])
  const [globalMemoryError, setGlobalMemoryError] = useState('')
  const [longTermMemories, setLongTermMemories] = useState<LongTermMemorySearchItem[]>([])
  const [longTermError, setLongTermError] = useState('')
  const searchSequence = useRef(new LatestRequest())

  useEffect(() => { setQuery(initialQuery) }, [initialQuery])

  const normalized = query.trim().toLocaleLowerCase('zh-CN')
  useEffect(() => {
    const sequence = searchSequence.current.begin()
    if (!normalized) {
      setGlobalMemoryItems([])
      setGlobalMemoryError('')
      setLongTermMemories([])
      setLongTermError('')
      return
    }
    const timer = window.setTimeout(() => {
      const searchParameters = new URLSearchParams({ q: query.trim(), workspace, limit: '40' })
      void api<GlobalMemorySearchResponse>(`/api/memories/search?${searchParameters}`).then(result => {
        if (searchSequence.current.isLatest(sequence)) {
          setGlobalMemoryItems(result.items)
          setGlobalMemoryError('')
        }
      }).catch(caught => {
        if (searchSequence.current.isLatest(sequence)) {
          setGlobalMemoryItems([])
          setGlobalMemoryError((caught as Error).message)
        }
      })
      void api<LongTermMemorySearchResponse>('/api/long-term-memories/search', {
        method: 'POST',
        body: JSON.stringify({ query: normalized, statuses: ['active'], sensitive_mode: 'exclude', limit: 20 }),
      }).then(result => {
        if (searchSequence.current.isLatest(sequence)) {
          setLongTermMemories(result.items)
          setLongTermError('')
        }
      }).catch(caught => {
        if (searchSequence.current.isLatest(sequence)) {
          setLongTermMemories([])
          setLongTermError((caught as Error).message)
        }
      })
    }, 180)
    return () => window.clearTimeout(timer)
  }, [normalized, query, workspace])
  const conversationResults = useMemo(() => normalized ? conversations.filter(item => item.title.toLocaleLowerCase('zh-CN').includes(normalized)) : [], [conversations, normalized])
  const memoryResults = useMemo(() => groupGlobalMemoryResults(globalMemoryItems), [globalMemoryItems])
  const projectMatches = normalized && `当前项目 ${workspace}`.toLocaleLowerCase('zh-CN').includes(normalized)
  const noResults = normalized && !conversationResults.length && !globalMemoryItems.length && !longTermMemories.length && !projectMatches && !globalMemoryError && !longTermError

  return <section className="search-panel">
    <header><h1>搜索</h1><p>查找对话、当前项目、全局记忆和个人长期记忆。</p></header>
    <label className="search-input"><Search size={19} /><span className="sr-only">搜索关键词</span><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="输入关键词" /></label>
    {!normalized && <div className="search-empty"><Search size={24} /><p>输入关键词开始搜索</p></div>}
    {normalized && <div className="search-results">
      {conversationResults.length > 0 && <section><h2>对话</h2>{conversationResults.map(item => <button key={item.id} onClick={() => onSelectConversation(item)}><MessageSquare size={17} /><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString('zh-CN')}</small></span></button>)}</section>}
      {projectMatches && <section><h2>项目</h2><button onClick={() => onNavigate('files')}><Folder size={17} /><span><strong>当前项目</strong><small>{workspace}</small></span></button></section>}
      {memoryResults.global.length > 0 && <section><h2>全局记忆</h2>{memoryResults.global.map(item => <button key={item.id} onClick={() => onNavigate('memory')}><Brain size={17} /><span><strong><HighlightedText text={item.key} terms={item.matched_terms} /></strong><small><HighlightedText text={item.content} terms={item.matched_terms} /></small></span></button>)}</section>}
      {memoryResults.project.length > 0 && <section><h2>当前项目记忆</h2>{memoryResults.project.map(item => <button key={item.id} onClick={() => onNavigate('memory')}><Brain size={17} /><span><strong><HighlightedText text={item.key} terms={item.matched_terms} /></strong><small><HighlightedText text={item.content} terms={item.matched_terms} /></small></span></button>)}</section>}
      {longTermMemories.length > 0 && <section><h2>个人长期记忆</h2>{longTermMemories.map(item => <button key={item.id} onClick={() => onOpenLongTermMemory(query.trim(), item.id)}><Brain size={17} /><span><strong><HighlightedText text={item.title || item.memory_type} terms={item.matched_terms} /></strong><small><HighlightedText text={item.content} terms={item.matched_terms} /></small></span></button>)}</section>}
      {globalMemoryError && <p className="panel-error">全局与项目记忆搜索失败：{globalMemoryError}</p>}
      {longTermError && <p className="panel-error">个人长期记忆搜索失败：{longTermError}</p>}
      {noResults && <div className="search-empty"><Search size={24} /><p>没有找到匹配结果</p></div>}
    </div>}
  </section>
}
