import { Brain, Folder, MessageSquare, Search } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Conversation, View, WorkspaceMemory } from '../types'

interface Props {
  conversations: Conversation[]
  workspace: string
  onSelectConversation: (item: Conversation) => void
  onNavigate: (view: View) => void
}

export function SearchPanel({ conversations, workspace, onSelectConversation, onNavigate }: Props) {
  const [query, setQuery] = useState('')
  const [memories, setMemories] = useState<WorkspaceMemory[]>([])
  const [memoryAvailable, setMemoryAvailable] = useState(true)

  useEffect(() => {
    api<WorkspaceMemory[]>(`/api/memories?workspace=${encodeURIComponent(workspace)}`)
      .then(items => { setMemories(items); setMemoryAvailable(true) })
      .catch(() => setMemoryAvailable(false))
  }, [workspace])

  const normalized = query.trim().toLocaleLowerCase('zh-CN')
  const conversationResults = useMemo(() => normalized ? conversations.filter(item => item.title.toLocaleLowerCase('zh-CN').includes(normalized)) : [], [conversations, normalized])
  const memoryResults = useMemo(() => normalized ? memories.filter(item => `${item.key} ${item.content} ${item.tags.join(' ')}`.toLocaleLowerCase('zh-CN').includes(normalized)).slice(0, 20) : [], [memories, normalized])
  const projectMatches = normalized && `当前项目 ${workspace}`.toLocaleLowerCase('zh-CN').includes(normalized)
  const noResults = normalized && !conversationResults.length && !memoryResults.length && !projectMatches

  return <section className="search-panel">
    <header><h1>搜索</h1><p>查找对话、当前项目和可用的工程记忆。</p></header>
    <label className="search-input"><Search size={19} /><span className="sr-only">搜索关键词</span><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="输入关键词" /></label>
    {!normalized && <div className="search-empty"><Search size={24} /><p>输入关键词开始搜索</p></div>}
    {normalized && <div className="search-results">
      {conversationResults.length > 0 && <section><h2>对话</h2>{conversationResults.map(item => <button key={item.id} onClick={() => onSelectConversation(item)}><MessageSquare size={17} /><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString('zh-CN')}</small></span></button>)}</section>}
      {projectMatches && <section><h2>项目</h2><button onClick={() => onNavigate('files')}><Folder size={17} /><span><strong>当前项目</strong><small>{workspace}</small></span></button></section>}
      {memoryResults.length > 0 && <section><h2>工程记忆</h2>{memoryResults.map(item => <button key={item.id} onClick={() => onNavigate('memory')}><Brain size={17} /><span><strong>{item.key}</strong><small>{item.content}</small></span></button>)}</section>}
      {!memoryAvailable && <p className="search-note">记忆搜索暂不可用</p>}
      {noResults && <div className="search-empty"><Search size={24} /><p>没有找到匹配结果</p></div>}
    </div>}
  </section>
}
