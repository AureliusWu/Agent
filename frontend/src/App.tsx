import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import {
  Bot, Check, ChevronRight, File, FilePlus2, Files, Folder, History,
  Gauge, Menu, MessageSquare, Paperclip, Plug, Plus, Send, Settings2, Shield,
  Sparkles, Upload, X,
} from 'lucide-react'
import { API_BASE, api } from './api'
import { isDesktop, saveDesktopSecret } from './secrets'
import type { ContextStats, Conversation, FileItem, Message, PendingAction, PermissionMode, ProviderHealth, View } from './types'
import './App.css'
import './Setup.css'

const modeLabel: Record<PermissionMode, string> = { readonly: '只读', confirm: '需确认', auto: '自动执行' }

function App() {
  const [view, setView] = useState<View>('chat')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [showSetup, setShowSetup] = useState(false)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [active, setActive] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [workspace, setWorkspace] = useState('<repository-parent>')
  const [mode, setMode] = useState<PermissionMode>('confirm')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [apiOnline, setApiOnline] = useState(false)
  const [pending, setPending] = useState<PendingAction[]>([])
  const [context, setContext] = useState<ContextStats | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const refreshConversations = () => api<Conversation[]>('/api/conversations').then(setConversations).catch(e => setError(e.message))
  useEffect(() => {
    api<{status:string}>('/api/health').then(result => setApiOnline(result.status === 'ok')).catch(() => setApiOnline(false))
    refreshConversations()
  }, [])
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, pending])

  async function createConversation() {
    setError('')
    try {
      const item = await api<Conversation>('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '新对话', workspace, permission_mode: mode }) })
      setConversations(old => [item, ...old])
      setActive(item); setMessages([]); setPending([]); setView('chat'); setSidebarOpen(false); setShowSetup(false)
    } catch (e) { setError((e as Error).message) }
  }

  async function changeMode(next: PermissionMode) {
    setMode(next)
    if (!active) return
    try {
      await api(`/api/conversations/${active.id}/permission`, { method: 'PATCH', body: JSON.stringify({ permission_mode: next }) })
      setActive({ ...active, permission_mode: next })
      setConversations(items => items.map(item => item.id === active.id ? { ...item, permission_mode: next } : item))
    } catch (e) { setError((e as Error).message) }
  }

  async function selectConversation(item: Conversation) {
    setActive(item); setMode(item.permission_mode); setWorkspace(item.workspace); setPending([]); setView('chat'); setSidebarOpen(false)
    try {
      const [loadedMessages, stats] = await Promise.all([api<Message[]>(`/api/conversations/${item.id}/messages`), api<ContextStats>(`/api/conversations/${item.id}/context`)])
      setMessages(loadedMessages); setContext(stats)
    } catch (e) { setError((e as Error).message) }
  }

  async function send(content = input, approvedActions: string[] = []) {
    if (!content.trim() || busy) return
    if (!active) { setError('请先创建对话并选择工作区'); return }
    setBusy(true); setError(''); setPending([])
    if (!approvedActions.length) { setMessages(old => [...old, { role: 'user', content }]); setInput('') }
    try {
      const result = await api<{ content: string; pending_actions: PendingAction[] }>('/api/chat', {
        method: 'POST', body: JSON.stringify({ conversation_id: active.id, content, approved_actions: approvedActions }),
      })
      setMessages(old => [...old, { role: 'assistant', content: result.content }])
      setPending(result.pending_actions || [])
      if ('context' in result) setContext((result as typeof result & { context: ContextStats }).context)
      refreshConversations()
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  function approve(action: PendingAction) {
    const lastUser = [...messages].reverse().find(item => item.role === 'user')?.content || '继续执行已确认操作'
    send(lastUser, [action.approval_key])
  }

  async function compactContext() {
    if (!active || busy) return
    setBusy(true); setError('')
    try { setContext(await api<ContextStats>(`/api/conversations/${active.id}/compact`, { method: 'POST', body: JSON.stringify({ force: true }) })) }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <div className="brand"><span className="brand-mark"><Sparkles size={18}/></span><strong>Agent</strong><button className="icon-btn mobile-only" onClick={() => setSidebarOpen(false)} aria-label="关闭"><X size={18}/></button></div>
        <button className="new-chat" onClick={() => setShowSetup(true)}><Plus size={17}/>新建对话</button>
        <div className="conversation-list">
          <span className="section-label">最近对话</span>
          {conversations.map(item => <button key={item.id} className={active?.id === item.id ? 'conversation active' : 'conversation'} onClick={() => selectConversation(item)}><MessageSquare size={15}/><span>{item.title}</span></button>)}
        </div>
        <div className="sidebar-bottom"><div className="workspace-mini"><Folder size={15}/><span title={workspace}>{workspace}</span></div><div className="api-status"><span className={apiOnline ? 'status-dot' : 'status-dot offline'}/>API {apiOnline ? '已连接' : '未连接'} · {API_BASE.replace(/^https?:\/\//, '')}</div></div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <button className="icon-btn mobile-only" onClick={() => setSidebarOpen(true)} aria-label="菜单"><Menu size={20}/></button>
          <div><h1>{active?.title || '通用 Agent'}</h1><p>{active ? active.workspace : '创建对话以开始工作'}</p></div>
          {active && <button className="context-meter" onClick={compactContext} title="压缩长对话上下文"><Gauge size={15}/><span>{context ? `约 ${context.estimated_tokens.toLocaleString()} tokens` : '上下文'}</span></button>}
          <div className="permission-switch" aria-label="权限模式">
            {(['readonly','confirm','auto'] as PermissionMode[]).map(item => <button key={item} className={mode === item ? 'active' : ''} onClick={() => changeMode(item)}>{modeLabel[item]}</button>)}
          </div>
        </header>

        {view === 'chat' && <section className="chat-view">
          <div className="messages">
            {!messages.length && <div className="welcome"><span><Bot size={30}/></span><h2>从一个明确任务开始</h2><p>我可以在选定工作区内读取、创建、修改和移动文件，也可以调用已挂载的 Skill 与 MCP 服务。</p><div className="suggestions"><button onClick={() => setInput('分析当前工作区并说明项目结构')}>分析项目结构</button><button onClick={() => setInput('检查当前项目的测试和风险')}>检查项目风险</button></div></div>}
            {messages.map((message, index) => <article key={index} className={`message ${message.role}`}><div className="avatar">{message.role === 'user' ? '你' : <Bot size={16}/>}</div><div className="message-body"><ReactMarkdown>{message.content}</ReactMarkdown></div></article>)}
            {busy && <div className="thinking"><span/><span/><span/> Agent 正在工作</div>}
            {pending.map(action => <div className="approval" key={action.approval_key}><div><Shield size={18}/><strong>需要确认：{action.tool}</strong><code>{JSON.stringify(action.arguments)}</code></div><div><button className="secondary" onClick={() => setPending([])}>拒绝</button><button className="primary" onClick={() => approve(action)}><Check size={15}/>批准</button></div></div>)}
            <div ref={endRef}/>
          </div>
          <form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); send() }}>
            {error && <div className="error-banner">{error}<button type="button" onClick={() => setError('')}><X size={14}/></button></div>}
            <textarea value={input} onChange={e => setInput(e.target.value)} placeholder="描述任务，Agent 只会访问已选择的工作区…" rows={3} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}/>
            <div className="composer-actions"><button type="button" className="icon-btn" onClick={() => setView('files')} title="上传文件"><Paperclip size={18}/></button><span>{modeLabel[mode]}模式</span><button className="send-btn" disabled={!input.trim() || busy} aria-label="发送"><Send size={18}/></button></div>
          </form>
        </section>}
        {view === 'files' && <FilesPanel active={active} workspace={workspace} mode={mode}/>}
        {view === 'extensions' && <ExtensionsPanel workspace={workspace}/>}
        {view === 'audit' && <AuditPanel/>}
      </main>

      <nav className="tool-rail">
        <RailButton active={view === 'chat'} icon={<MessageSquare/>} label="对话" onClick={() => setView('chat')}/>
        <RailButton active={view === 'files'} icon={<Files/>} label="文件" onClick={() => setView('files')}/>
        <RailButton active={view === 'extensions'} icon={<Plug/>} label="扩展" onClick={() => setView('extensions')}/>
        <RailButton active={view === 'audit'} icon={<History/>} label="审计" onClick={() => setView('audit')}/>
      </nav>
      {showSetup && <div className="modal-backdrop" role="presentation"><div className="setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title"><div className="dialog-title"><div><h2 id="setup-title">创建 Agent 对话</h2><p>明确选择本次允许访问的工作区</p></div><button className="icon-btn" onClick={() => setShowSetup(false)} aria-label="关闭"><X size={18}/></button></div><label>工作区绝对路径<input value={workspace} onChange={event => setWorkspace(event.target.value)} placeholder="<workspace-root>"/></label><fieldset><legend>权限模式</legend><div className="setup-modes">{(['readonly','confirm','auto'] as PermissionMode[]).map(item => <button type="button" key={item} className={mode===item?'active':''} onClick={()=>setMode(item)}>{modeLabel[item]}</button>)}</div></fieldset><div className="dialog-actions"><button className="secondary" onClick={() => setShowSetup(false)}>取消</button><button className="primary" onClick={createConversation}><Plus size={16}/>创建对话</button></div></div></div>}
    </div>
  )
}

function RailButton({active, icon, label, onClick}: {active:boolean; icon:React.ReactNode; label:string; onClick:()=>void}) { return <button className={active ? 'active' : ''} onClick={onClick}>{icon}<span>{label}</span></button> }

function PanelHeader({icon, title, subtitle}: {icon:React.ReactNode; title:string; subtitle:string}) { return <header className="panel-header"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header> }

function FilesPanel({active, workspace, mode}: {active: Conversation|null; workspace:string; mode:PermissionMode}) {
  const [items,setItems]=useState<FileItem[]>([]); const [path,setPath]=useState('.'); const [error,setError]=useState(''); const fileRef=useRef<HTMLInputElement>(null)
  const activeId = active?.id
  const load=async(next=path)=>{try{const result=await api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:active?.id,workspace,permission_mode:mode,tool:'list_files',arguments:{path:next}})});setItems(result.items);setPath(next);setError('')}catch(e){setError((e as Error).message)}}
  useEffect(()=>{if(!activeId)return;api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:activeId,workspace,permission_mode:mode,tool:'list_files',arguments:{path:'.'}})}).then(result=>{setItems(result.items);setPath('.');setError('')}).catch(error=>setError(error.message))},[activeId,workspace,mode])
  async function upload(file:File){const target=path==='.'?file.name:`${path}/${file.name}`;const form=new FormData();form.append('file',file);const query=new URLSearchParams({workspace,path:target,permission_mode:mode,approved:String(mode==='auto')});try{const response=await fetch(`${API_BASE}/api/files/upload?${query}`,{method:'POST',body:form});const result=await response.json();if(result.status==='confirmation_required'){if(!confirm(`上传 ${file.name} 到 ${target}？`))return;query.set('approved','true');await fetch(`${API_BASE}/api/files/upload?${query}`,{method:'POST',body:form})}load()}catch(e){setError((e as Error).message)}}
  return <section className="content-panel"><PanelHeader icon={<Files/>} title="工作区文件" subtitle={workspace}/><div className="panel-toolbar"><button className="primary" onClick={()=>fileRef.current?.click()}><Upload size={16}/>上传文件</button><input ref={fileRef} hidden type="file" onChange={e=>e.target.files?.[0]&&upload(e.target.files[0])}/><button className="secondary" onClick={()=>load()}><Settings2 size={16}/>刷新</button></div>{error&&<p className="panel-error">{error}</p>}<div className="file-list">{path!=='.'&&<button onClick={()=>load(path.split('/').slice(0,-1).join('/')||'.')}><Folder/><span>..</span></button>}{items.map(item=><button key={item.path} onClick={()=>item.type==='directory'&&load(item.path)}><span className="file-icon">{item.type==='directory'?<Folder/>:<File/>}</span><span>{item.name}</span><small>{item.size?`${Math.ceil(item.size/1024)} KB`:''}</small>{item.type==='directory'&&<ChevronRight/>}</button>)}</div></section>
}

function ExtensionsPanel({workspace}:{workspace:string}) {
  const [skills,setSkills]=useState<{name:string;description:string;path:string}[]>([]); const [mcps,setMcps]=useState<{id:number;name:string;transport:string;url:string}[]>([]); const [name,setName]=useState(''); const [url,setUrl]=useState(''); const [modelKey,setModelKey]=useState(''); const [keyStatus,setKeyStatus]=useState(''); const [health,setHealth]=useState<ProviderHealth|null>(null)
  const load=()=>{api<typeof skills>(`/api/skills?workspace=${encodeURIComponent(workspace)}`).then(setSkills).catch(()=>{});api<typeof mcps>('/api/mcp').then(setMcps).catch(()=>{})}; useEffect(load,[workspace])
  async function addMcp(e:FormEvent){e.preventDefault();await api('/api/mcp',{method:'POST',body:JSON.stringify({name,transport:'http',url,args:[]})});setName('');setUrl('');load()}
  async function checkHealth(){try{setHealth(await api<ProviderHealth>('/api/provider/health'))}catch(error){setHealth({status:'error',latency_ms:null,model:'',error:(error as Error).message})}}
  async function saveKey(e:FormEvent){e.preventDefault();try{await saveDesktopSecret('model_api_key',modelKey);setModelKey('');setKeyStatus('已保存到 Windows 凭据管理器');setTimeout(checkHealth,150)}catch(error){setKeyStatus((error as Error).message)}}
  return <section className="content-panel"><PanelHeader icon={<Plug/>} title="扩展能力" subtitle="模型、Skill 与 MCP"/><div className="extension-grid"><div><h3>模型凭据</h3><form className="mcp-form" onSubmit={saveKey}><input type="password" placeholder="DeepSeek / OpenAI-compatible API Key" value={modelKey} onChange={e=>setModelKey(e.target.value)} required/><button className="primary" disabled={!isDesktop()}><Shield size={16}/>{isDesktop()?'保存到 Windows 凭据':'网页端由后端环境变量管理'}</button>{keyStatus&&<p>{keyStatus}</p>}</form><button className="secondary health-button" onClick={checkHealth}><Gauge size={16}/>检查模型连接</button>{health&&<p className={`provider-health ${health.status}`}>{health.status==='ok'?`${health.model} 可用 · ${health.latency_ms} ms`:health.status==='unconfigured'?'尚未配置 API Key':`连接失败：${health.error}`}</p>}<h3>已挂载 Skill <span>{skills.length}</span></h3>{skills.length?skills.map(item=><div className="extension-row" key={item.path}><span><Sparkles/></span><div><strong>{item.name}</strong><p>{item.description||item.path}</p></div></div>):<div className="empty-panel"><FilePlus2/><p>在工作区 `.agent/skills/*/SKILL.md` 添加 Skill</p></div>}</div><div><h3>MCP 服务 <span>{mcps.length}</span></h3>{mcps.map(item=><div className="extension-row" key={item.id}><span><Plug/></span><div><strong>{item.name}</strong><p>{item.transport} · {item.url}</p></div></div>)}<form className="mcp-form" onSubmit={addMcp}><input placeholder="服务名称" value={name} onChange={e=>setName(e.target.value)} required/><input placeholder="https://mcp.example.com/mcp" value={url} onChange={e=>setUrl(e.target.value)} required/><button className="primary"><Plus size={16}/>连接 HTTP MCP</button></form></div></div></section>
}

function AuditPanel(){const [logs,setLogs]=useState<{id:number;action:string;target:string;status:string;created_at:string}[]>([]);useEffect(()=>{api<typeof logs>('/api/audit').then(setLogs).catch(()=>{})},[]);return <section className="content-panel"><PanelHeader icon={<History/>} title="操作审计" subtitle="文件、模型与 MCP 调用记录"/><div className="audit-list">{logs.map(log=><div key={log.id}><span className={`audit-status ${log.status}`}/><div><strong>{log.action}</strong><p>{log.target||'系统操作'}</p></div><time>{new Date(log.created_at).toLocaleString()}</time><code>{log.status}</code></div>)}</div></section>}

export default App
