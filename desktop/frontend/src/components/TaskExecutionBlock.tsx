import { Check, ChevronDown, Globe2, Play, RotateCcw, Shield } from 'lucide-react'
import type { PendingAction, RecoverableTask, RuntimeEvent, VerificationReport } from '../types'

interface Props {
  pending: PendingAction[]
  runtimeEvents: RuntimeEvent[]
  verification: VerificationReport | null
  recoverable: RecoverableTask | null
  selectedCheckpoint: number | null
  workspaceDrift: boolean
  uncertainOperation: boolean
  onResume: (options?: { allowWorkspaceDrift?: boolean; retryUncertain?: boolean; checkpointSequence?: number }) => void
  onAbandon: () => void
  onCheckpoint: (sequence: number) => void
  onApprove: (action: PendingAction, scope?: 'once' | 'task' | 'session') => void
  onReject: () => void
}

export function TaskExecutionBlock(props: Props) {
  return <div className="task-blocks">
    {props.runtimeEvents.length > 0 && <details className="task-block runtime-timeline" open>
      <summary><Globe2 size={17} /><strong>执行时间线</strong><span>{props.runtimeEvents.length} 项</span><ChevronDown size={16} /></summary>
      <div className="task-block-body runtime-event-list">{props.runtimeEvents.map(item => <div key={item.id}>
        <code>{item.event}</code><span>{String(item.payload.provider || item.payload.reason || item.payload.tool || '')}</span>
        {item.event === 'search.completed' && <small>{String(item.payload.result_count ?? 0)} 条来源 · {String(item.payload.duration_ms ?? '-')} ms</small>}
      </div>)}</div>
    </details>}
    {props.recoverable && <details className="task-block recovery-block" open>
      <summary><RotateCcw size={17} /><strong>可继续任务</strong><span>{props.recoverable.current_phase}</span><ChevronDown size={16} /></summary>
      <div className="task-block-body">
        <p>{props.recoverable.termination_reason || '任务现场和检查点已保留。'}</p>
        <label>检查点
          <select value={props.selectedCheckpoint || ''} onChange={event => props.onCheckpoint(Number(event.target.value))}>
            {props.recoverable.checkpoints.map(item => <option value={item.sequence} key={item.sequence}>#{item.sequence} {item.phase} · {item.reason}</option>)}
          </select>
        </label>
        <div className="task-block-actions">
          <button className="secondary" onClick={props.onAbandon}>放弃</button>
          {props.workspaceDrift ? <button className="primary" onClick={() => props.onResume({ allowWorkspaceDrift: true })}>确认当前现场</button> : props.uncertainOperation ? <button className="primary" onClick={() => props.onResume({ retryUncertain: true })}>确认重试操作</button> : <button className="primary" onClick={() => props.onResume()}><Play size={15} />继续</button>}
        </div>
      </div>
    </details>}

    {props.pending.map(action => <details className="task-block approval-block" open key={action.approval_key}>
      <summary><Shield size={17} /><strong>需要确认：{action.tool}</strong><span>{action.risk || 'high'}</span><ChevronDown size={16} /></summary>
      <div className="task-block-body">
        <p>{action.impact || '该操作将影响当前工作区。'}{action.source ? ` 来源：${action.source}` : ''}</p>
        <code>{JSON.stringify(action.arguments)}</code>
        <div className="task-block-actions">
          <button className="secondary" onClick={props.onReject}>拒绝</button>
          {action.allowed_scopes?.includes('session') && <button className="secondary" onClick={() => props.onApprove(action, 'session')}>本会话允许</button>}
          {action.allowed_scopes?.includes('task') && <button className="secondary" onClick={() => props.onApprove(action, 'task')}>本任务允许</button>}
          <button className="primary" onClick={() => props.onApprove(action, 'once')}><Check size={15} />允许一次</button>
        </div>
      </div>
    </details>)}
  </div>
}
