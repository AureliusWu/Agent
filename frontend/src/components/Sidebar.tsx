import { Folder, MessageSquare, Pencil, Plus, Sparkles, Trash2, X } from 'lucide-react'
import { API_BASE } from '../api'
import type { Conversation } from '../types'
import '../styles/sidebar.css'

interface Props {
  open: boolean
  conversations: Conversation[]
  active: Conversation | null
  workspace: string
  apiOnline: boolean
  onClose: () => void
  onNew: () => void
  onSelect: (item: Conversation) => void
  onRename: (item: Conversation) => void
  onDelete: (item: Conversation) => void
}

export function Sidebar(props: Props) {
  return <aside className={`sidebar ${props.open ? 'open' : ''}`}>
    <div className="brand"><span className="brand-mark"><Sparkles size={18}/></span><strong>Agent</strong><button className="icon-btn mobile-only" onClick={props.onClose} aria-label="关闭"><X size={18}/></button></div>
    <button className="new-chat" onClick={props.onNew}><Plus size={17}/>新建对话</button>
    <div className="conversation-list">
      <span className="section-label">最近对话</span>
      {props.conversations.map(item => <div key={item.id} className={props.active?.id === item.id ? 'conversation active' : 'conversation'}>
        <button onClick={() => props.onSelect(item)}><MessageSquare size={15}/><span>{item.title}</span></button>
        <button title="重命名" onClick={() => props.onRename(item)}><Pencil size={13}/></button>
        <button title="删除" onClick={() => props.onDelete(item)}><Trash2 size={13}/></button>
      </div>)}
    </div>
    <div className="sidebar-bottom"><div className="workspace-mini"><Folder size={15}/><span title={props.workspace}>{props.workspace}</span></div><div className="api-status"><span className={props.apiOnline ? 'status-dot' : 'status-dot offline'}/>API {props.apiOnline ? '已连接' : '未连接'} · {API_BASE.replace(/^https?:\/\//, '')}</div></div>
  </aside>
}
