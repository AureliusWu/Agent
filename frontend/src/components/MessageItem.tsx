import ReactMarkdown from 'react-markdown'
import adminAvatar from '../assets/admin-avatar.svg'
import { CHARACTER_ASSETS } from '../characterAssets'
import type { Message } from '../types'

function messageTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function MessageItem({ message }: { message: Message }) {
  const isAdmin = message.role === 'user'
  const name = isAdmin ? '管理员' : '夏目心'
  return <article className={`message-item ${message.role}`} aria-label={`${name}的消息`}>
    <img className="message-avatar" src={isAdmin ? adminAvatar : CHARACTER_ASSETS.natsumeKokoro.imageSrc} alt="" />
    <div className="message-column">
      <header className="message-meta">
        <strong>{name}</strong>
        {message.created_at && <time>{messageTime(message.created_at)}</time>}
      </header>
      {message.reasoning && <details className="message-reasoning">
        <summary>思考过程</summary>
        <div><ReactMarkdown>{message.reasoning}</ReactMarkdown></div>
      </details>}
      <div className="message-document"><ReactMarkdown>{message.content}</ReactMarkdown></div>
    </div>
  </article>
}
