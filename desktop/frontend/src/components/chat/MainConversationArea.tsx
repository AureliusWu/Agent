import type { RefObject } from 'react'
import { CHARACTER_ASSETS } from '../../characterAssets'
import type { CommandDefinition, ContextStats, ConversationQueueItem, Message, PendingAction, PermissionMode, ReasoningEffort, RecoverableTask, RuntimeEvent, TokenUsage, VerificationReport, View } from '../../types'
import { Composer } from './Composer'
import { MessageItem } from './MessageItem'
import { TaskExecutionBlock } from '../tasks/TaskExecutionBlock'

export interface MainConversationAreaProps {
  messages: Message[]
  pending: PendingAction[]
  runtimeEvents: RuntimeEvent[]
  input: string
  busy: boolean
  error: string
  mode: PermissionMode
  reasoningEffort: ReasoningEffort
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  hasConversation: boolean
  queuedItems: ConversationQueueItem[]
  commands: CommandDefinition[]
  verification: VerificationReport | null
  usage: TokenUsage | null
  context: ContextStats | null
  recoverable: RecoverableTask | null
  selectedCheckpoint: number | null
  workspaceDrift: boolean
  uncertainOperation: boolean
  endRef: RefObject<HTMLDivElement | null>
  onInput: (value: string) => void
  onMode: (mode: PermissionMode) => void | Promise<void>
  onReasoningEffort: (value: ReasoningEffort) => void
  onPreferredModel: (value: string) => void
  onSend: () => void
  onSteer: () => void
  onPromoteQueued: (itemId: string) => void | Promise<void>
  onCancelQueued: (itemId: string) => void | Promise<void>
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
          <p>{props.hasConversation ? '夏目心正在等待你的任务。' : '新建任务后可直接聊天，需要操作文件时再打开项目。'}</p>
        </div>}
        {props.messages.map((message, index) => <MessageItem key={message.id || `${message.role}-${index}`} message={message} />)}
        {props.busy && <div className="thinking-line" role="status"><span /><span /><span /><p>夏目心正在处理当前任务</p></div>}
        <TaskExecutionBlock pending={props.pending} runtimeEvents={props.runtimeEvents} verification={props.verification} recoverable={props.recoverable} selectedCheckpoint={props.selectedCheckpoint} workspaceDrift={props.workspaceDrift} uncertainOperation={props.uncertainOperation} onResume={props.onResume} onAbandon={props.onAbandon} onCheckpoint={props.onCheckpoint} onApprove={props.onApprove} onReject={props.onReject} />
        <div ref={props.endRef} />
      </div>
    </div>
    <Composer input={props.input} busy={props.busy} error={props.error} usage={props.usage} context={props.context} mode={props.mode} reasoningEffort={props.reasoningEffort} preferredModel={props.preferredModel} defaultModel={props.defaultModel} modelOptions={props.modelOptions} hasConversation={props.hasConversation} queuedItems={props.queuedItems} commands={props.commands} onInput={props.onInput} onMode={props.onMode} onReasoningEffort={props.onReasoningEffort} onPreferredModel={props.onPreferredModel} onSend={props.onSend} onSteer={props.onSteer} onPromoteQueued={props.onPromoteQueued} onCancelQueued={props.onCancelQueued} onStop={props.onStop} onNavigate={props.onNavigate} onUploadFile={props.onUploadFile} onClearError={props.onClearError} />
  </section>
}
