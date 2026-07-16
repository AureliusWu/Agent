import type { RefObject } from 'react'
import { CHARACTER_ASSETS } from '../characterAssets'
import type { AgentProfile, Message, OrchestrationMode, PendingAction, PermissionMode, ReasoningEffort, RecoverableTask, VerificationReport, View } from '../types'
import { Composer } from './Composer'
import { MessageItem } from './MessageItem'
import { TaskExecutionBlock } from './TaskExecutionBlock'

export interface MainConversationAreaProps {
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
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  hasConversation: boolean
  verification: VerificationReport | null
  recoverable: RecoverableTask | null
  selectedCheckpoint: number | null
  workspaceDrift: boolean
  uncertainOperation: boolean
  endRef: RefObject<HTMLDivElement | null>
  onInput: (value: string) => void
  onMode: (mode: PermissionMode) => void | Promise<void>
  onProfile: (value: string) => void
  onOrchestration: (value: OrchestrationMode) => void
  onReasoningEffort: (value: ReasoningEffort) => void
  onPreferredModel: (value: string) => void
  onSend: () => void
  onPause: () => void
  onStop: () => void
  onResume: (options?: { allowWorkspaceDrift?: boolean; retryUncertain?: boolean; checkpointSequence?: number }) => void
  onAbandon: () => void
  onCheckpoint: (sequence: number) => void
  onNavigate: (view: View) => void
  onUploadFile: (file: File) => void | Promise<void>
  onApprove: (action: PendingAction, scope?: 'once' | 'task' | 'session') => void
  onReject: () => void
  onClearError: () => void
}

export function MainConversationArea(props: MainConversationAreaProps) {
  return <section className="main-conversation-area">
    <header className="conversation-heading">
      <h1>主对话与任务区</h1>
      <p>与夏目心协作，推进当前任务。</p>
    </header>
    <div className="conversation-scroll">
      <div className="conversation-feed">
        {!props.messages.length && <div className="conversation-empty">
          <img src={CHARACTER_ASSETS.natsumeKokoro.imageSrc} alt="" />
          <strong>从一件具体的事开始</strong>
          <p>{props.hasConversation ? '夏目心正在等待你的任务。' : '新建任务并选择工作区后即可开始。'}</p>
        </div>}
        {props.messages.map((message, index) => <MessageItem key={message.id || `${message.role}-${index}`} message={message} />)}
        {props.busy && <div className="thinking-line" role="status"><span /><span /><span /><p>夏目心正在处理当前任务</p></div>}
        <TaskExecutionBlock pending={props.pending} verification={props.verification} recoverable={props.recoverable} selectedCheckpoint={props.selectedCheckpoint} workspaceDrift={props.workspaceDrift} uncertainOperation={props.uncertainOperation} onResume={props.onResume} onAbandon={props.onAbandon} onCheckpoint={props.onCheckpoint} onApprove={props.onApprove} onReject={props.onReject} />
        <div ref={props.endRef} />
      </div>
    </div>
    <Composer input={props.input} busy={props.busy} error={props.error} mode={props.mode} profiles={props.profiles} agentProfileId={props.agentProfileId} orchestrationMode={props.orchestrationMode} reasoningEffort={props.reasoningEffort} preferredModel={props.preferredModel} defaultModel={props.defaultModel} modelOptions={props.modelOptions} hasConversation={props.hasConversation} onInput={props.onInput} onMode={props.onMode} onProfile={props.onProfile} onOrchestration={props.onOrchestration} onReasoningEffort={props.onReasoningEffort} onPreferredModel={props.onPreferredModel} onSend={props.onSend} onPause={props.onPause} onStop={props.onStop} onNavigate={props.onNavigate} onUploadFile={props.onUploadFile} onClearError={props.onClearError} />
  </section>
}
