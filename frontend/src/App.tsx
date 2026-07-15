/* oxlint-disable react-hooks/exhaustive-deps */
import { useEffect, useState } from 'react'
import { api, getApiBase, getConfiguredApiAddress } from './api'
import { ACTIVE_CONVERSATION_KEY, MODE_KEY, savedMode } from './constants'
import { useAgentChat } from './hooks/useAgentChat'
import { AuditPanel } from './components/AuditPanel'
import { ChatView } from './components/ChatView'
import { ExtensionsPanel } from './components/ExtensionsPanel'
import { FilesPanel } from './components/FilesPanel'
import { SetupDialog } from './components/SetupDialog'
import { Sidebar } from './components/Sidebar'
import { ToolRail } from './components/ToolRail'
import { Topbar } from './components/Topbar'
import type { AgentProfile, Conversation, PermissionMode, View } from './types'
import './App.css'

function App() {
  const [view, setView] = useState<View>('chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showSetup, setShowSetup] = useState(false)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [workspace, setWorkspace] = useState('<repository-parent>')
  const [mode, setMode] = useState<PermissionMode>(savedMode)
  const [profiles, setProfiles] = useState<AgentProfile[]>([])
  const [profileId, setProfileId] = useState('general')
  const [apiOnline, setApiOnline] = useState(false)
  const [apiAddress, setApiAddress] = useState(getConfiguredApiAddress())
  const [webAccessToken, setWebAccessToken] = useState('')

  const refreshConversations = () => api<Conversation[]>('/api/conversations').then(setConversations).catch(error => chat.setError(error.message))
  const refreshProfiles = () => api<AgentProfile[]>('/api/agent-profiles').then(setProfiles).catch(error => chat.setError(error.message))
  const chat = useAgentChat(active, refreshConversations)

  // Initial hydration intentionally runs once; subsequent changes are driven by user actions.
  useEffect(() => {
    getApiBase().then(base=>setApiAddress(base.replace(/^https?:\/\//,''))).catch(error=>chat.setError(error.message))
    api<{status:string}>('/api/health').then(result => setApiOnline(result.status === 'ok')).catch(error => { setApiOnline(false); chat.setError(`本地后端不可用：${error.message}`) })
    refreshProfiles()
    api<Conversation[]>('/api/conversations').then(async items => {
      setConversations(items)
      const savedId = Number(localStorage.getItem(ACTIVE_CONVERSATION_KEY))
      const item = items.find(candidate => candidate.id === savedId)
      if (!item) return
      setActive(item)
      setMode(item.permission_mode)
      setWorkspace(item.workspace)
      setProfileId(item.agent_profile_id || 'general')
      localStorage.setItem(MODE_KEY, item.permission_mode)
      await chat.loadConversation(item)
    }).catch(error => chat.setError(error.message))
  }, [])

  async function createConversation() {
    chat.setError('')
    try {
      const item = await api<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '新对话', workspace, permission_mode: mode, agent_profile_id: profileId }) })
      setConversations(old => [item, ...old])
      setActive(item)
      chat.resetConversation()
      setView('chat')
      setSidebarOpen(false)
      setShowSetup(false)
      localStorage.setItem(ACTIVE_CONVERSATION_KEY, String(item.id))
      localStorage.setItem(MODE_KEY, item.permission_mode)
    } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function changeMode(next: PermissionMode) {
    const previous = mode
    setMode(next)
    localStorage.setItem(MODE_KEY, next)
    if (!active) return
    try {
      await api(`/api/conversations/${active.id}/permission`, { method: 'PATCH', body: JSON.stringify({ permission_mode: next }) })
      setActive({ ...active, permission_mode: next })
      setConversations(items => items.map(item => item.id === active.id ? { ...item, permission_mode: next } : item))
    } catch (caught) {
      setMode(previous)
      localStorage.setItem(MODE_KEY, previous)
      chat.setError(`权限模式保存失败：${(caught as Error).message}`)
    }
  }

  async function changeProfile(next: string) {
    const previous = profileId
    setProfileId(next)
    if (!active) return
    try {
      await api(`/api/conversations/${active.id}/profile`, { method: 'PATCH', body: JSON.stringify({ agent_profile_id: next }) })
      setActive({ ...active, agent_profile_id: next })
      setConversations(items => items.map(item => item.id === active.id ? { ...item, agent_profile_id: next } : item))
    } catch (caught) {
      setProfileId(previous)
      chat.setError(`专业模式保存失败：${(caught as Error).message}`)
    }
  }

  async function selectConversation(item: Conversation) {
    if (chat.busy) await chat.stopTask()
    setActive(item)
    setMode(item.permission_mode)
    setWorkspace(item.workspace)
    setProfileId(item.agent_profile_id || 'general')
    setView('chat')
    setSidebarOpen(false)
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, String(item.id))
    localStorage.setItem(MODE_KEY, item.permission_mode)
    try { await chat.loadConversation(item) } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function renameConversation(item: Conversation) {
    const title = prompt('新的对话名称', item.title)?.trim()
    if (!title) return
    try {
      await api(`/api/conversations/${item.id}`, { method: 'PATCH', body: JSON.stringify({ title }) })
      setConversations(items=>items.map(value=>value.id===item.id?{...value,title}:value))
      if(active?.id===item.id)setActive({...active,title})
    } catch(caught){chat.setError((caught as Error).message)}
  }

  async function removeConversation(item: Conversation) {
    if(!confirm(`删除对话“${item.title}”？`))return
    try {
      await api(`/api/conversations/${item.id}`, {method:'DELETE'})
      setConversations(items=>items.filter(value=>value.id!==item.id))
      if(active?.id===item.id){setActive(null);chat.resetConversation();localStorage.removeItem(ACTIVE_CONVERSATION_KEY)}
    } catch(caught){chat.setError((caught as Error).message)}
  }

  return <div className="app-shell">
    <Sidebar open={sidebarOpen} conversations={conversations} active={active} workspace={workspace} apiOnline={apiOnline} apiAddress={apiAddress} onClose={()=>setSidebarOpen(false)} onNew={()=>setShowSetup(true)} onSelect={selectConversation} onRename={renameConversation} onDelete={removeConversation}/>
    <main className="main-area">
      <Topbar active={active} mode={mode} context={chat.context} onMenu={()=>setSidebarOpen(true)} onMode={changeMode} onCompact={chat.compactContext}/>
      {view==='chat'&&<ChatView messages={chat.messages} pending={chat.pending} verification={chat.verification} recoverable={chat.recoverable} selectedCheckpoint={chat.selectedCheckpoint} workspaceDrift={chat.workspaceDrift} uncertainOperation={chat.uncertainOperation} input={chat.input} busy={chat.busy} error={chat.error} mode={mode} profiles={profiles} agentProfileId={profileId} orchestrationMode={chat.orchestrationMode} endRef={chat.endRef} onInput={chat.setInput} onProfile={changeProfile} onOrchestration={chat.setOrchestrationMode} onSend={()=>chat.send()} onPause={chat.pauseTask} onStop={chat.stopTask} onResume={chat.resumeTask} onAbandon={chat.abandonRecovery} onCheckpoint={chat.setSelectedCheckpoint} onFiles={()=>setView('files')} onApprove={chat.approve} onReject={chat.abandonRecovery} onClearError={()=>chat.setError('')}/>}
      {view==='files'&&<FilesPanel active={active} workspace={workspace} mode={mode}/>}
      {view==='extensions'&&<ExtensionsPanel workspace={workspace} onChanged={refreshProfiles}/>}
      {view==='audit'&&<AuditPanel/>}
    </main>
    <ToolRail view={view} onView={setView}/>
    {showSetup&&<SetupDialog workspace={workspace} mode={mode} profiles={profiles} agentProfileId={profileId} webAccessToken={webAccessToken} onWorkspace={setWorkspace} onMode={setMode} onProfile={setProfileId} onWebAccessToken={setWebAccessToken} onClose={()=>setShowSetup(false)} onCreate={createConversation}/>}
  </div>
}

export default App
