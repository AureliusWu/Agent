import { ChevronDown, Minimize2 } from 'lucide-react'
import { useState } from 'react'
import type { ContextStats, VerificationReport } from '../types'

interface Props {
  hasConversation: boolean
  context: ContextStats | null
  busy: boolean
  pendingCount: number
  recoverable: boolean
  verification: VerificationReport | null
  onCompact: () => void
}

function taskStatus(props: Props): string {
  if (props.busy) return '正在执行'
  if (props.pendingCount > 0) return '等待确认'
  if (props.recoverable) return '任务可继续'
  return props.hasConversation ? '待命' : '未开始'
}

export function ConversationSummary(props: Props) {
  const [expanded, setExpanded] = useState(true)
  return <section className={expanded ? 'summary-module expanded' : 'summary-module'}>
    <header>
      <button className="summary-heading-button" onClick={() => setExpanded(value => !value)} aria-expanded={expanded}>
        <strong>摘要</strong><ChevronDown size={16} />
      </button>
      <button className="line-icon-button compact-button" disabled={!props.hasConversation || props.busy} onClick={props.onCompact} title="压缩当前上下文" aria-label="压缩当前上下文"><Minimize2 size={15} /></button>
    </header>
    {expanded && <div className="summary-body">
      <p>{props.context?.summary?.trim() || '暂无摘要'}</p>
      <dl><dt>状态</dt><dd>{taskStatus(props)}</dd></dl>
    </div>}
  </section>
}
