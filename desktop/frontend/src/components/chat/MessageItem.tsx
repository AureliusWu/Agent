import ReactMarkdown from 'react-markdown'
import { TerminalSquare } from 'lucide-react'
import adminAvatar from '../../assets/admin-avatar.svg'
import { CHARACTER_ASSETS } from '../../characterAssets'
import { extractArtifactDownloads, mergeArtifactDownloads } from '../../shared/artifactDownloads'
import type { Message } from '../../types'
import { ReasoningTimeline } from '../tasks/ReasoningTimeline'
import { ArtifactDownloadCard, AuthenticatedArtifactLink } from './ArtifactDownloadCard'

function messageTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

export function MessageItem({ message }: { message: Message }) {
  const isAdmin = message.role === 'user'
  const isSystem = message.role === 'system'
  const name = isSystem ? '本地指令' : isAdmin ? '管理员' : '夏目心'
  const artifacts = mergeArtifactDownloads(message.artifacts || [], extractArtifactDownloads(message))
  return <article className={`message-item ${message.role}`} aria-label={`${name}的消息`}>
    {isSystem ? <span className="message-system-icon"><TerminalSquare size={19} /></span> : <span className={`message-avatar ${isAdmin ? 'admin' : 'kokoro'}`} aria-hidden="true"><img src={isAdmin ? adminAvatar : CHARACTER_ASSETS.natsumeKokoro.imageSrc} alt="" /></span>}
    <div className="message-column">
      <header className="message-meta">
        <strong>{name}</strong>
        {message.created_at && <time>{messageTime(message.created_at)}</time>}
      </header>
      {message.reasoning && <ReasoningTimeline content={message.reasoning} />}
      <div className="message-document"><ReactMarkdown components={{
        a: ({ href = '', children }) => <AuthenticatedArtifactLink href={href}>{children}</AuthenticatedArtifactLink>,
      }}>{message.content}</ReactMarkdown></div>
      {artifacts.length > 0 && <div className="message-artifacts">
        {artifacts.map(artifact => <ArtifactDownloadCard key={artifact.artifactId} artifact={artifact} />)}
      </div>}
    </div>
  </article>
}
