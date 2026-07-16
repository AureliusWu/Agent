import { Folder, MessageSquare, Plus, Search, X } from 'lucide-react'
import type { Conversation, View } from '../types'
import { RecentConversationList } from './RecentConversationList'

interface Props {
  open: boolean
  view: View
  conversations: Conversation[]
  active: Conversation | null
  onClose: () => void
  onNew: () => void
  onNavigate: (view: View) => void
  onSelect: (item: Conversation) => void
  onRename: (item: Conversation) => void
  onDelete: (item: Conversation) => void
}

export function CollapsibleSidebar(props: Props) {
  return <aside className={props.open ? 'workbench-sidebar open' : 'workbench-sidebar'} aria-label="工作台导航">
    <div className="sidebar-mobile-head">
      <strong>导航</strong>
      <button className="line-icon-button" onClick={props.onClose} aria-label="关闭左侧栏"><X size={18} /></button>
    </div>
    <button className="new-task-button" onClick={props.onNew}><Plus size={20} />新建任务</button>
    <nav className="primary-navigation">
      <button className={props.view === 'files' ? 'active' : ''} onClick={() => props.onNavigate('files')}><Folder size={19} />项目</button>
      <button className={props.view === 'chat' ? 'active' : ''} onClick={() => props.onNavigate('chat')}><MessageSquare size={19} />聊天</button>
      <button className={props.view === 'search' ? 'active' : ''} onClick={() => props.onNavigate('search')}><Search size={19} />搜索</button>
    </nav>
    <div className="sidebar-rule" />
    <RecentConversationList conversations={props.conversations} active={props.active} onSelect={props.onSelect} onRename={props.onRename} onDelete={props.onDelete} />
  </aside>
}
