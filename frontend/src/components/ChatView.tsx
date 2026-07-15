import type { FormEvent, RefObject } from 'react'
import ReactMarkdown from 'react-markdown'
import { AlertTriangle, Check, CheckCircle2, Pause, Paperclip, Play, RotateCcw, Send, Shield, Sparkles, Square, UserRound, Waves, X } from 'lucide-react'
import { MODE_LABEL, ORCHESTRATION_LABEL } from '../constants'
import type { AgentProfile, Message, OrchestrationMode, PendingAction, PermissionMode, ReasoningEffort, RecoverableTask, VerificationReport } from '../types'
import '../styles/chat.css'

interface Props {
  messages: Message[]
  pending: PendingAction[]
  input: string
  busy: boolean
  error: string
  mode: PermissionMode
  profiles: AgentProfile[]
  agentProfileId: string
  orchestrationMode: OrchestrationMode
  reasoningEffort: ReasoningEffort
  verification: VerificationReport | null
  recoverable: RecoverableTask | null
  selectedCheckpoint: number | null
  workspaceDrift: boolean
  uncertainOperation: boolean
  endRef: RefObject<HTMLDivElement | null>
  onInput: (value: string) => void
  onProfile: (value: string) => void
  onOrchestration: (value: OrchestrationMode) => void
  onReasoningEffort: (value: ReasoningEffort) => void
  onSend: () => void
  onPause: () => void
  onStop: () => void
  onResume: (options?: { allowWorkspaceDrift?: boolean; retryUncertain?: boolean; checkpointSequence?: number }) => void
  onAbandon: () => void
  onCheckpoint: (sequence: number) => void
  onFiles: () => void
  onApprove: (action: PendingAction, scope?: 'once' | 'task' | 'session') => void
  onReject: () => void
  onClearError: () => void
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

export function ChatView(props: Props) {
  return <section className="chat-view">
    <div className="messages">
      {!props.messages.length && <div className="welcome">
        <div className="welcome-sigil"><Waves size={30} /></div>
        <span className="welcome-kicker">MEMORY OCEAN · ONLINE</span>
        <h2>晚上好，管理员。</h2>
        <p>夏目心负责理解、表达与陪伴，司忆负责工具、任务、文件和记忆。告诉她你想完成什么，系统会在当前工作区和权限边界内行动。</p>
        <div className="welcome-status"><span>夏目心已连接</span><span>司忆核心按需执行</span><span>工程记忆可追溯</span></div>
        <div className="suggestions"><button onClick={() => props.onInput('分析当前工作区并说明项目结构')}>分析当前工作区</button><button onClick={() => props.onInput('检查当前项目的测试、风险和下一步')}>检查项目风险</button><button onClick={() => props.onInput('读取工程记忆并总结当前项目状态')}>读取工程记忆</button></div>
      </div>}
      {props.messages.map((message, index) => <article key={index} className={`message ${message.role}`}>
        <div className="message-marker"><span>{message.role === 'user' ? <UserRound size={15} /> : <Sparkles size={15} />}</span></div>
        <div className="message-content">
          <header><strong>{message.role === 'user' ? '管理员' : '夏目心'}</strong><span>{message.role === 'user' ? 'COMMAND' : 'KOKORO RESPONSE'}</span></header>
          <div className="message-body"><ReactMarkdown>{message.content}</ReactMarkdown></div>
        </div>
      </article>)}
      {props.busy && <div className="thinking"><span /><span /><span /> 夏目心正在回应，司忆正在执行当前任务</div>}
      {props.recoverable && !props.busy && <section className="recovery-strip"><div><RotateCcw size={18} /><strong>可继续任务</strong><span>{props.recoverable.current_phase} · {props.recoverable.termination_reason || props.recoverable.status}</span></div><label>检查点<select value={props.selectedCheckpoint || ''} onChange={event => props.onCheckpoint(Number(event.target.value))}>{props.recoverable.checkpoints.map(item => <option value={item.sequence} key={item.sequence}>#{item.sequence} {item.phase} · {item.reason}</option>)}</select></label><div className="recovery-actions">{props.workspaceDrift ? <button className="primary" onClick={() => props.onResume({ allowWorkspaceDrift: true })}>确认当前现场</button> : props.uncertainOperation ? <button className="primary" onClick={() => props.onResume({ retryUncertain: true })}>确认重试操作</button> : <button className="primary" onClick={() => props.onResume()}><Play size={15} />继续</button>}<button className="secondary" onClick={props.onAbandon}>放弃</button></div></section>}
      {props.pending.map(action => <div className="approval" key={action.approval_key}><div><Shield size={18} /><strong>司忆请求确认：{action.tool}</strong><p>风险：{action.risk || 'high'} · 影响：{action.impact || '当前工作区'}{action.source ? ` · 来源：${action.source}` : ''}</p><code>{JSON.stringify(action.arguments)}</code></div><div><button className="secondary" onClick={props.onReject}>拒绝</button>{action.allowed_scopes?.includes('session') && <button className="secondary" onClick={() => props.onApprove(action, 'session')}>本会话</button>}{action.allowed_scopes?.includes('task') && <button className="secondary" onClick={() => props.onApprove(action, 'task')}>本任务</button>}<button className="primary" onClick={() => props.onApprove(action, 'once')}><Check size={15} />允许一次</button></div></div>)}
      {props.verification && <section className={`verification-card ${props.verification.status}`}><header>{props.verification.status === 'passed' ? <CheckCircle2 /> : <AlertTriangle />}<strong>{props.verification.summary}</strong><span>{props.verification.evaluation ? `${props.verification.evaluation.score} 分 · ` : ''}{props.verification.checks.filter(item => item.status === 'passed').length}/{props.verification.checks.length} 项通过</span></header>{props.verification.checks.map((check, index) => <div key={index}><span className={`audit-status ${check.status === 'passed' ? 'ok' : 'error'}`} /><code>{check.requirement_id || check.criterion_id || check.kind}</code><p>{check.description || (typeof check.target === 'string' ? check.target : JSON.stringify(check.target))}</p><small title={evidenceSummary(check.evidence)}>{check.verifier || 'CoreVerifier'} · {check.status}{check.reason ? ` · ${check.reason}` : ''} · {evidenceSummary(check.evidence)}</small></div>)}</section>}
      <div ref={props.endRef} />
    </div>
    <form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); props.onSend() }}>
      {props.error && <div className="error-banner">{props.error}<button type="button" onClick={props.onClearError}><X size={14} /></button></div>}
      <div className="composer-status"><strong>{props.busy ? '夏目心正在工作' : '夏目心等待指令'}</strong><span>司忆权限 · {MODE_LABEL[props.mode]}</span></div>
      <textarea value={props.input} onChange={event => props.onInput(event.target.value)} placeholder="告诉夏目心你想完成什么……" rows={3} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); props.onSend() } }} />
      <div className="composer-actions"><button type="button" className="icon-btn" onClick={props.onFiles} title="上传文件"><Paperclip size={18} /></button><span>{MODE_LABEL[props.mode]}</span><label className="reasoning-select"><span className="sr-only">推理强度</span><select disabled={props.busy} value={props.reasoningEffort} onChange={event => props.onReasoningEffort(event.target.value as ReasoningEffort)}><option value="auto">自动推理</option><option value="low">低推理</option><option value="medium">中推理</option><option value="high">高推理</option></select></label><label className="agent-profile-select"><span className="sr-only">专业 Agent 模式</span><select disabled={props.busy} value={props.agentProfileId} onChange={event => props.onProfile(event.target.value)}>{!props.profiles.some(item => item.id === props.agentProfileId) && <option value={props.agentProfileId}>{props.agentProfileId} (已停用)</option>}{props.profiles.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label className="orchestration-select"><span className="sr-only">Agent 协作模式</span><select disabled={props.busy} value={props.orchestrationMode} onChange={event => props.onOrchestration(event.target.value as OrchestrationMode)}>{(Object.keys(ORCHESTRATION_LABEL) as OrchestrationMode[]).map(value => <option key={value} value={value}>{ORCHESTRATION_LABEL[value]}</option>)}</select></label>{props.busy ? <div className="task-controls"><button type="button" className="pause-btn" onClick={props.onPause} title="暂停并保留检查点"><Pause size={14} /><span>暂停</span></button><button type="button" className="stop-btn" onClick={props.onStop} title="停止任务"><Square size={14} /><span>停止</span></button></div> : <button className="send-btn" disabled={!props.input.trim()} aria-label="发送"><Send size={18} /></button>}</div>
    </form>
  </section>
}
