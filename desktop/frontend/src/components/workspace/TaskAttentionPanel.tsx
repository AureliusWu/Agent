import { ChevronRight, ListTodo, RefreshCw } from 'lucide-react'
import { useTaskAttention } from '../../hooks/useTaskAttention'
import { attentionStatusLabels } from '../../shared/taskAttention'
import type { TaskAttentionTarget } from '../../shared/taskAttention'
import '../../styles/task-attention.css'

interface Props {
  activityRevision: string
  onOpenTask: (target: TaskAttentionTarget) => void
}

const updatedTime = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })

export function TaskAttentionPanel({ activityRevision, onOpenTask }: Props) {
  const { snapshot, loading, failed, refresh } = useTaskAttention(activityRevision)
  const settled = !loading && !failed
  return <div className="workbench-attention">
    <div className="workbench-section-heading">
      <h2>人工待办</h2>
      <button type="button" className="line-icon-button" aria-label="刷新人工待办" title="刷新" disabled={loading} onClick={() => { void refresh() }}><RefreshCw size={15} /></button>
    </div>
    <p className="workbench-note">查看全部对话中等待确认或恢复的任务。打开任务不会批准或执行操作。</p>
    {failed && <p className="panel-error" role="alert">{snapshot ? '刷新失败。下方为上次结果，刷新后可重新打开任务。' : '无法读取人工待办，请刷新重试。'}</p>}
    {loading && <p className="workbench-note" role="status">正在核对待办…</p>}
    {snapshot && snapshot.total === 0 && settled && <div className="workbench-empty"><ListTodo size={26} aria-hidden="true" /><p role="status">没有需要处理的任务。</p></div>}
    {snapshot && snapshot.total > 0 && <>
      <p className="attention-count" role="status">{failed ? '上次结果 · ' : ''}{snapshot.total.toLocaleString()} 项待办{snapshot.total > snapshot.items.length && ` · 显示前 ${snapshot.items.length} 项`}</p>
      <ul className="attention-list" aria-label="需要人工处理的任务" aria-busy={loading}>
        {snapshot.items.map(item => <li key={item.task_id}>
          <button type="button" aria-label={`打开对话 ${item.conversation_id} 的任务 ${item.task_id}：${attentionStatusLabels[item.status]}`} disabled={!settled} onClick={() => { if (settled) onOpenTask({ taskId: item.task_id, conversationId: item.conversation_id }) }}>
            <span className="attention-entry"><strong>{attentionStatusLabels[item.status]}</strong><span>对话 #{item.conversation_id} · 任务 {item.task_id.slice(0, 8)}</span></span>
            <time dateTime={item.updated_at}>{updatedTime.format(new Date(item.updated_at))}</time>
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </li>)}
      </ul>
    </>}
  </div>
}
