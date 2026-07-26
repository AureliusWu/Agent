import { Folder, MessageSquare, Plus, Search, X } from 'lucide-react'
import type { Conversation, View } from '../../types'
import type { BuildInfo } from '../../buildInfo'
import { RecentConversationList } from '../chat/RecentConversationList'

interface Props {
  open: boolean
  view: View
  conversations: Conversation[]
  active: Conversation | null
  buildInfo: BuildInfo
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
      <button className={props.view === 'projects' ? 'active' : ''} onClick={() => props.onNavigate('projects')}><Folder size={19} />项目</button>
      <button className={props.view === 'chat' ? 'active' : ''} onClick={() => props.onNavigate('chat')}><MessageSquare size={19} />聊天</button>
      <button className={props.view === 'search' ? 'active' : ''} onClick={() => props.onNavigate('search')}><Search size={19} />搜索</button>
    </nav>
    <div className="sidebar-rule" />
    <RecentConversationList conversations={props.conversations} active={props.active} onSelect={props.onSelect} onRename={props.onRename} onDelete={props.onDelete} />
    <div className={`sidebar-build ${props.buildInfo.consistency.status === 'mismatch' ? 'mismatch' : ''}`} title={props.buildInfo.consistency.status === 'mismatch' ? '构建不一致，请在“关于司忆”中查看详情' : '当前构建指纹'}>
      v{props.buildInfo.react.product_version} · {props.buildInfo.react.git_short_commit} · {new Date(props.buildInfo.react.build_time).toLocaleString()}
    </div>
  </aside>
}
