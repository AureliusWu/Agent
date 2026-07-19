/* oxlint-disable react-hooks/exhaustive-deps */
import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft } from 'lucide-react'
import { api, apiFetch, clearDesktopApiCache, getApiBase, getConfiguredApiAddress } from './api'
import { ACTIVE_CONVERSATION_KEY, MODE_KEY, SIDEBAR_KEY, WORKSPACE_KEY, savedMode } from './constants'
import { getDesktopBackendStatus, restartDesktopBackend } from './desktopRuntime'
import type { DesktopBackendHealth } from './desktopRuntime'
import { useAgentChat } from './hooks/useAgentChat'
import { useMediaQuery } from './hooks/useMediaQuery'
import { useBuildInfo } from './buildInfo'
import { AboutPanel } from './components/AboutPanel'
import { AuditPanel } from './components/AuditPanel'
import { ChatView } from './components/ChatView'
import { CollapsibleSidebar } from './components/CollapsibleSidebar'
import { DesktopStatusBar } from './components/DesktopStatusBar'
import { ExtensionsPanel } from './components/ExtensionsPanel'
import { FilesPanel } from './components/FilesPanel'
import { MemoryPanel } from './components/MemoryPanel'
import { ProjectsPanel } from './components/ProjectsPanel'
import { SearchPanel } from './components/SearchPanel'
import { TopHeader } from './components/TopHeader'
import { UsagePanel } from './components/UsagePanel'
import type { Conversation, PermissionMode, ProviderPolicy, View } from './types'
import './App.css'

interface UploadResult {
  status: string
  approval_key?: string
  error?: string
}

function App() {
  const [view, setView] = useState<View>('chat')
  const [desktopSidebarExpanded, setDesktopSidebarExpanded] = useState(() => localStorage.getItem(SIDEBAR_KEY) !== 'false')
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [workspace, setWorkspace] = useState(() => localStorage.getItem(WORKSPACE_KEY) || '')
  const [mode, setMode] = useState<PermissionMode>(savedMode)
  const [providerPolicy, setProviderPolicy] = useState<ProviderPolicy | null>(null)
  const [apiOnline, setApiOnline] = useState(false)
  const [apiAddress, setApiAddress] = useState(getConfiguredApiAddress())
  const [backendHealth, setBackendHealth] = useState<DesktopBackendHealth | null>(null)
  const [restartingBackend, setRestartingBackend] = useState(false)
  const isMobile = useMediaQuery('(max-width: 899px)')
  const buildInfo = useBuildInfo()

  const refreshConversations = () => api<Conversation[]>('/api/conversations').then(setConversations).catch(error => chat.setError(error.message))
  const chat = useAgentChat(active, refreshConversations)
  const sidebarExpanded = isMobile ? mobileSidebarOpen : desktopSidebarExpanded
  const modelOptions = useMemo(() => [...new Set(Object.values(providerPolicy?.models || {}).filter(Boolean))], [providerPolicy])
  const defaultModel = providerPolicy?.models.medium || providerPolicy?.models.strong || providerPolicy?.models.light || '自动路由'

  useEffect(() => {
    if (buildInfo.environment !== 'Desktop') return
    let mounted = true
    const refresh = async () => {
      try {
        const status = await getDesktopBackendStatus()
        if (mounted) {
          setBackendHealth(status)
          setApiOnline(Boolean(status?.ready))
        }
      } catch (error) {
        if (mounted) {
          setApiOnline(false)
          chat.setError(`本地核心状态读取失败：${(error as Error).message}`)
        }
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [buildInfo.environment])

  useEffect(() => {
    getApiBase().then(base => setApiAddress(base.replace(/^https?:\/\//, ''))).catch(error => chat.setError(error.message))
    api<{ status: string }>('/api/health').then(result => setApiOnline(result.status === 'ok')).catch(error => { setApiOnline(false); chat.setError(`本地后端不可用：${error.message}`) })
    api<ProviderPolicy>('/api/provider/policy').then(setProviderPolicy).catch(error => chat.setError(error.message))
    api<Conversation[]>('/api/conversations').then(async items => {
      setConversations(items)
      const savedId = Number(localStorage.getItem(ACTIVE_CONVERSATION_KEY))
      const item = items.find(candidate => candidate.id === savedId)
      if (!item) return
      setActive(item)
      setMode(item.permission_mode)
      setWorkspace(item.workspace)
      localStorage.setItem(MODE_KEY, item.permission_mode)
      localStorage.setItem(WORKSPACE_KEY, item.workspace)
      await chat.loadConversation(item)
    }).catch(error => chat.setError(error.message))
  }, [])

  async function restartCore() {
    setRestartingBackend(true)
    chat.setError('')
    clearDesktopApiCache()
    try {
      const status = await restartDesktopBackend()
      setBackendHealth(status)
      setApiOnline(true)
      await api<ProviderPolicy>('/api/provider/policy').then(setProviderPolicy)
    } catch (caught) {
      setApiOnline(false)
      chat.setError(`本地核心重启失败：${(caught as Error).message}`)
      setBackendHealth(await getDesktopBackendStatus().catch(() => null))
    } finally {
      setRestartingBackend(false)
    }
  }

  function toggleSidebar() {
    if (isMobile) {
      setMobileSidebarOpen(value => !value)
      return
    }
    setDesktopSidebarExpanded(value => {
      const next = !value
      localStorage.setItem(SIDEBAR_KEY, String(next))
      return next
    })
  }

  function navigate(next: View) {
    setView(next)
    if (isMobile) setMobileSidebarOpen(false)
  }

  async function createConversation(selectedWorkspace = workspace) {
    chat.setError('')
    selectedWorkspace = selectedWorkspace.trim()
    try {
      const item = await api<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '新对话', workspace: selectedWorkspace, permission_mode: mode, agent_profile_id: 'general' }) })
      setConversations(old => [item, ...old])
      setActive(item)
      setWorkspace(selectedWorkspace)
      chat.resetConversation()
      navigate('chat')
      localStorage.setItem(ACTIVE_CONVERSATION_KEY, String(item.id))
      localStorage.setItem(MODE_KEY, item.permission_mode)
      if (selectedWorkspace) localStorage.setItem(WORKSPACE_KEY, selectedWorkspace)
      else localStorage.removeItem(WORKSPACE_KEY)
    } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function openProject(projectWorkspace: string) {
    const existing = conversations.find(item => item.workspace === projectWorkspace)
    if (existing) await selectConversation(existing)
    else await createConversation(projectWorkspace)
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

  async function selectConversation(item: Conversation) {
    if (chat.busy) await chat.stopTask()
    setActive(item)
    setMode(item.permission_mode)
    setWorkspace(item.workspace)
    navigate('chat')
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, String(item.id))
    localStorage.setItem(MODE_KEY, item.permission_mode)
    localStorage.setItem(WORKSPACE_KEY, item.workspace)
    try { await chat.loadConversation(item) } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function renameConversation(item: Conversation) {
    const title = prompt('新的对话名称', item.title)?.trim()
    if (!title) return
    try {
      await api(`/api/conversations/${item.id}`, { method: 'PATCH', body: JSON.stringify({ title }) })
      setConversations(items => items.map(value => value.id === item.id ? { ...value, title } : value))
      if (active?.id === item.id) setActive({ ...active, title })
    } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function removeConversation(item: Conversation) {
    if (!confirm(`删除对话“${item.title}”？`)) return
    try {
      await api(`/api/conversations/${item.id}`, { method: 'DELETE' })
      setConversations(items => items.filter(value => value.id !== item.id))
      if (active?.id === item.id) { setActive(null); chat.resetConversation(); localStorage.removeItem(ACTIVE_CONVERSATION_KEY) }
    } catch (caught) { chat.setError((caught as Error).message) }
  }

  async function uploadFile(file: File) {
    if (!active || !workspace) {
      chat.setError('请先在“项目”中打开一个工作区。')
      return
    }
    const send = async (token?: string) => {
      const form = new FormData()
      form.append('file', file)
      const query = new URLSearchParams({ workspace, path: file.name, permission_mode: mode, conversation_id: String(active.id) })
      if (token) query.set('approval_token', token)
      const response = await apiFetch(`/api/files/upload?${query}`, { method: 'POST', body: form })
      const result = await response.json() as UploadResult
      if (!response.ok) throw new Error(result.error || `上传失败（HTTP ${response.status}）`)
      return result
    }
    try {
      let result = await send()
      if (result.status === 'confirmation_required') {
        if (!confirm(`上传 ${file.name} 到当前项目？`)) return
        result = await send(result.approval_key)
      }
      if (result.status !== 'ok') throw new Error(result.error || '上传失败')
      navigate('files')
    } catch (caught) {
      chat.setError((caught as Error).message)
    }
  }

  const secondaryContent = view === 'search'
    ? <SearchPanel conversations={conversations} workspace={workspace} onSelectConversation={selectConversation} onNavigate={navigate} />
    : view === 'projects'
      ? <ProjectsPanel conversations={conversations} onOpen={openProject} />
    : view === 'memory'
      ? <MemoryPanel workspace={workspace} />
      : view === 'usage'
        ? <UsagePanel />
      : view === 'files'
        ? <FilesPanel active={active} workspace={workspace} mode={mode} />
        : view === 'extensions'
          ? <ExtensionsPanel workspace={workspace} onChanged={() => undefined} />
          : view === 'audit'
            ? <AuditPanel />
            : <AboutPanel buildInfo={buildInfo} apiOnline={apiOnline} apiAddress={apiAddress} workspace={workspace} />

  return <div className="app-shell">
    <TopHeader sidebarExpanded={sidebarExpanded} onToggleSidebar={toggleSidebar} />
    {buildInfo.environment === 'Desktop' && <DesktopStatusBar version={buildInfo.version} model={chat.preferredModel || defaultModel} busy={chat.busy} health={backendHealth} restarting={restartingBackend} onRestart={restartCore} />}
    <div className={sidebarExpanded ? 'workbench sidebar-expanded' : 'workbench sidebar-collapsed'}>
      <CollapsibleSidebar open={sidebarExpanded} view={view} conversations={conversations} active={active} buildInfo={buildInfo} onClose={() => setMobileSidebarOpen(false)} onNew={() => { void createConversation('') }} onNavigate={navigate} onSelect={selectConversation} onRename={renameConversation} onDelete={removeConversation} />
      <main className="main-area">
        {view === 'chat' ? <ChatView messages={chat.messages} pending={chat.pending} runtimeEvents={chat.runtimeEvents} verification={chat.verification} usage={chat.usage} context={chat.context} recoverable={chat.recoverable} selectedCheckpoint={chat.selectedCheckpoint} workspaceDrift={chat.workspaceDrift} uncertainOperation={chat.uncertainOperation} input={chat.input} busy={chat.busy} error={chat.error} mode={mode} reasoningEffort={chat.reasoningEffort} preferredModel={chat.preferredModel} defaultModel={defaultModel} modelOptions={modelOptions} hasConversation={Boolean(active)} queuedItems={chat.queued} endRef={chat.endRef} onInput={chat.setInput} onMode={changeMode} onReasoningEffort={chat.setReasoningEffort} onPreferredModel={chat.setPreferredModel} onSend={() => chat.send()} onSteer={() => chat.steer()} onPromoteQueued={chat.promoteQueued} onCancelQueued={chat.cancelQueued} onStop={chat.stopTask} onResume={chat.resumeTask} onAbandon={chat.abandonRecovery} onCheckpoint={chat.setSelectedCheckpoint} onNavigate={navigate} onUploadFile={uploadFile} onApprove={chat.approve} onReject={chat.abandonRecovery} onClearError={() => chat.setError('')} /> : <section className="secondary-page"><button className="back-to-chat" onClick={() => navigate('chat')}><ArrowLeft size={16} />返回对话</button>{secondaryContent}</section>}
      </main>
    </div>
    {isMobile && mobileSidebarOpen && <button className="drawer-scrim" onClick={() => setMobileSidebarOpen(false)} aria-label="关闭左侧栏" />}
  </div>
}

export default App
