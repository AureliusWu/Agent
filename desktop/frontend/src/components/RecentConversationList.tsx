import { MessageSquare, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import type { Conversation } from '../types'

interface Props {
  conversations: Conversation[]
  active: Conversation | null
  onSelect: (item: Conversation) => void
  onRename: (item: Conversation) => void
  onDelete: (item: Conversation) => void
}

function conversationTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const today = new Date()
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }).replace('/', '-')
}

export function RecentConversationList({ conversations, active, onSelect, onRename, onDelete }: Props) {
  return <div className="recent-conversations">
    <h2>最近对话</h2>
    <div className="recent-list">
      {conversations.length === 0 && <p className="sidebar-empty">暂无对话</p>}
      {conversations.map(item => <div className={active?.id === item.id ? 'recent-item active' : 'recent-item'} key={item.id}>
        <button className="recent-main" onClick={() => onSelect(item)}>
          <MessageSquare size={16} />
          <span>{item.title}</span>
          <time>{conversationTime(item.updated_at)}</time>
        </button>
        <details className="recent-more">
          <summary aria-label={`${item.title}的更多操作`}><MoreHorizontal size={16} /></summary>
          <div className="recent-menu">
            <button onClick={() => onRename(item)}><Pencil size={14} />重命名</button>
            <button onClick={() => onDelete(item)}><Trash2 size={14} />删除</button>
          </div>
        </details>
      </div>)}
    </div>
  </div>
}
