import { AlertTriangle, Check, CheckCircle2, ChevronDown, Play, RotateCcw, Shield } from 'lucide-react'
import type { PendingAction, RecoverableTask, VerificationReport } from '../types'

interface Props {
  pending: PendingAction[]
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

function evidenceSummary(value: unknown): string {
  if (!value) return '无证据'
  if (Array.isArray(value)) {
    if (!value.length) return '无证据'
    const first = value[0] as Record<string, unknown>
    if (typeof first?.command === 'string') return first.command
    const paths = value.map(item => String((item as Record<string, unknown>)?.path || '')).filter(Boolean)
    return paths.length ? paths.join('、') : `${value.length} 条证据`
  }
  if (typeof value === 'object') return `${Object.keys(value as Record<string, unknown>).length} 项证据`
  return String(value)
}

const verificationLabel: Record<VerificationReport['status'], string> = {
  passed: '验证通过',
  partially_passed: '验证不完整',
  failed: '验证失败',
  blocked: '任务已阻塞',
}

export function TaskExecutionBlock(props: Props) {
  return <div className="task-blocks">
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

    {props.verification && <details className={`task-block verification-block ${props.verification.status}`} open>
      <summary>{props.verification.status === 'passed' ? <CheckCircle2 size={17} /> : <AlertTriangle size={17} />}<strong>{verificationLabel[props.verification.status]}</strong><span>{props.verification.evaluation ? `${props.verification.evaluation.score} 分 · ` : ''}{props.verification.checks.filter(item => item.status === 'passed').length}/{props.verification.checks.length}</span><ChevronDown size={16} /></summary>
      <div className="verification-list">
        <p className="verification-summary">{props.verification.summary}</p>
        {props.verification.checks.map((check, index) => <div className="verification-row" key={index}>
          <span className={`verification-mark ${check.status}`} aria-label={check.status} />
          <code>{check.requirement_id || check.criterion_id || check.kind}</code>
          <p>{check.description || (typeof check.target === 'string' ? check.target : JSON.stringify(check.target))}</p>
          <small title={evidenceSummary(check.evidence)}>{check.verifier || 'CoreVerifier'} · {evidenceSummary(check.evidence)}</small>
        </div>)}
      </div>
    </details>}
  </div>
}
