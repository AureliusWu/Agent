import type { FormEvent, RefObject } from 'react'
import ReactMarkdown from 'react-markdown'
import { AlertTriangle, Bot, Check, CheckCircle2, Pause, Paperclip, Play, RotateCcw, Send, Shield, Square, X } from 'lucide-react'
import { MODE_LABEL } from '../constants'
import type { Message, PendingAction, PermissionMode, RecoverableTask, VerificationReport } from '../types'
import '../styles/chat.css'

interface Props {
  messages: Message[]
  pending: PendingAction[]
  input: string
  busy: boolean
  error: string
  mode: PermissionMode
  verification: VerificationReport | null
  recoverable: RecoverableTask | null
  selectedCheckpoint: number | null
  workspaceDrift: boolean
  uncertainOperation: boolean
  endRef: RefObject<HTMLDivElement | null>
  onInput: (value:string)=>void
  onSend: ()=>void
  onPause: ()=>void
  onStop: ()=>void
  onResume: (options?:{allowWorkspaceDrift?:boolean; retryUncertain?:boolean; checkpointSequence?:number})=>void
  onAbandon: ()=>void
  onCheckpoint: (sequence:number)=>void
  onFiles: ()=>void
  onApprove: (action:PendingAction, scope?:'once'|'task'|'session')=>void
  onReject: ()=>void
  onClearError: ()=>void
}

export function ChatView(props: Props) {
  return <section className="chat-view">
    <div className="messages">
      {!props.messages.length && <div className="welcome"><span><Bot size={30}/></span><h2>从一个明确任务开始</h2><p>我可以在选定工作区内读取、创建、修改和移动文件，也可以调用已挂载的 Skill 与 MCP 服务。</p><div className="suggestions"><button onClick={() => props.onInput('分析当前工作区并说明项目结构')}>分析项目结构</button><button onClick={() => props.onInput('检查当前项目的测试和风险')}>检查项目风险</button></div></div>}
      {props.messages.map((message, index) => <article key={index} className={`message ${message.role}`}><div className="avatar">{message.role === 'user' ? '你' : <Bot size={16}/>}</div><div className="message-body"><ReactMarkdown>{message.content}</ReactMarkdown></div></article>)}
      {props.busy && <div className="thinking"><span/><span/><span/> Agent 正在工作，可随时停止</div>}
      {props.recoverable && !props.busy && <section className="recovery-strip"><div><RotateCcw size={18}/><strong>可继续任务</strong><span>{props.recoverable.current_phase} · {props.recoverable.termination_reason || props.recoverable.status}</span></div><label>检查点<select value={props.selectedCheckpoint || ''} onChange={event=>props.onCheckpoint(Number(event.target.value))}>{props.recoverable.checkpoints.map(item=><option value={item.sequence} key={item.sequence}>#{item.sequence} {item.phase} · {item.reason}</option>)}</select></label><div className="recovery-actions">{props.workspaceDrift?<button className="primary" onClick={()=>props.onResume({allowWorkspaceDrift:true})}>确认当前现场</button>:props.uncertainOperation?<button className="primary" onClick={()=>props.onResume({retryUncertain:true})}>确认重试操作</button>:<button className="primary" onClick={()=>props.onResume()}><Play size={15}/>继续</button>}<button className="secondary" onClick={props.onAbandon}>放弃</button></div></section>}
      {props.pending.map(action => <div className="approval" key={action.approval_key}><div><Shield size={18}/><strong>需要确认：{action.tool}</strong><p>风险：{action.risk || 'high'} · 影响：{action.impact || '当前工作区'}{action.source ? ` · 来源：${action.source}` : ''}</p><code>{JSON.stringify(action.arguments)}</code></div><div><button className="secondary" onClick={props.onReject}>拒绝</button>{action.allowed_scopes?.includes('session')&&<button className="secondary" onClick={() => props.onApprove(action,'session')}>本会话</button>}{action.allowed_scopes?.includes('task')&&<button className="secondary" onClick={() => props.onApprove(action,'task')}>本任务</button>}<button className="primary" onClick={() => props.onApprove(action,'once')}><Check size={15}/>允许一次</button></div></div>)}
      {props.verification && <section className={`verification-card ${props.verification.status}`}><header>{props.verification.status==='passed'?<CheckCircle2/>:<AlertTriangle/>}<strong>{props.verification.summary}</strong><span>{props.verification.evaluation?`${props.verification.evaluation.score} 分 · `:''}{props.verification.checks.filter(item=>item.status==='passed').length}/{props.verification.checks.length} 项通过</span></header>{props.verification.checks.map((check,index)=><div key={index}><span className={`audit-status ${check.status==='passed'?'ok':'error'}`}/><code>{check.criterion_id||check.kind}</code><p>{check.description||(typeof check.target==='string'?check.target:JSON.stringify(check.target))}</p><small>{check.status}{check.reason?` · ${check.reason}`:''}</small></div>)}</section>}
      <div ref={props.endRef}/>
    </div>
    <form className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); props.onSend() }}>
      {props.error && <div className="error-banner">{props.error}<button type="button" onClick={props.onClearError}><X size={14}/></button></div>}
      <textarea value={props.input} onChange={event => props.onInput(event.target.value)} placeholder="描述任务，Agent 只会访问已选择的工作区…" rows={3} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); props.onSend() } }}/>
      <div className="composer-actions"><button type="button" className="icon-btn" onClick={props.onFiles} title="上传文件"><Paperclip size={18}/></button><span>{MODE_LABEL[props.mode]}</span>{props.busy ? <div className="task-controls"><button type="button" className="pause-btn" onClick={props.onPause} title="暂停并保留检查点"><Pause size={14}/><span>暂停</span></button><button type="button" className="stop-btn" onClick={props.onStop} title="停止任务"><Square size={14}/><span>停止</span></button></div> : <button className="send-btn" disabled={!props.input.trim()} aria-label="发送"><Send size={18}/></button>}</div>
    </form>
  </section>
}
