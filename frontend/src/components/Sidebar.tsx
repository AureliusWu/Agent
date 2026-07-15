import { Folder, MessageSquare, Pencil, Plus, Sparkles, Trash2, X } from 'lucide-react'
import type { BuildInfo } from '../buildInfo'
import type { Conversation } from '../types'
import '../styles/sidebar.css'

interface Props {
  open: boolean
  conversations: Conversation[]
  active: Conversation | null
  workspace: string
  apiOnline: boolean
  apiAddress: string
  buildInfo: BuildInfo
  onClose: () => void
  onNew: () => void
  onSelect: (item: Conversation) => void
  onRename: (item: Conversation) => void
  onDelete: (item: Conversation) => void
}

export function Sidebar(props: Props) {
  return <aside className={`sidebar ${props.open ? 'open' : ''}`}>
    <div className="brand">
      <span className="brand-mark"><Sparkles size={18} /></span>
      <div className="brand-copy"><strong>MEMORY OCEAN</strong><span>记忆海终端</span></div>
      <button className="icon-btn mobile-only" onClick={props.onClose} aria-label="关闭"><X size={18} /></button>
    </div>
    <button className="new-chat" onClick={props.onNew}><Plus size={17} />唤醒新对话</button>
    <div className="conversation-list">
      <span className="section-label">Recent Sessions</span>
      {props.conversations.map(item => <div key={item.id} className={props.active?.id === item.id ? 'conversation active' : 'conversation'}>
        <button onClick={() => props.onSelect(item)}><MessageSquare size={15} /><span>{item.title}</span></button>
        <button title="重命名" onClick={() => props.onRename(item)}><Pencil size={13} /></button>
        <button title="删除" onClick={() => props.onDelete(item)}><Trash2 size={13} /></button>
      </div>)}
    </div>
    <div className="sidebar-bottom">
      <div className="api-status" title={props.apiAddress}><span className={props.apiOnline ? 'status-dot' : 'status-dot offline'} /><span>司忆核心 · {props.apiOnline ? '在线' : '离线'}</span></div>
      <div className="workspace-mini"><Folder size={14} /><span title={props.workspace}>{props.workspace}</span></div>
      <div className="version-mini"><span>v{props.buildInfo.version} · {props.buildInfo.environment}</span></div>
    </div>
  </aside>
}
