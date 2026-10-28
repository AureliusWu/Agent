import type { RefObject } from 'react'
import keyLogo from '../../assets/kokoro-key.svg'
import type { CommandDefinition, ContextStats, ConversationQueueItem, Message, OrchestrationMode, PendingAction, PermissionMode, ReasoningEffort, RecoverableTask, RuntimeEvent, TokenUsage, VerificationReport, View } from '../../types'
import { Composer } from './Composer'
import { ConversationContextBar } from './ConversationContextBar'
import type { WorkspaceChoice } from './ConversationContextBar'
import { MessageItem } from './MessageItem'
import { TaskExecutionBlock } from '../tasks/TaskExecutionBlock'
import type { StopState } from '../../hooks/useAgentChat'

export interface MainConversationAreaProps {
  messages: Message[]
  pending: PendingAction[]
  runtimeEvents: RuntimeEvent[]
  input: string
  busy: boolean
  stopState: StopState
  error: string
  mode: PermissionMode
  reasoningEffort: ReasoningEffort
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  hasConversation: boolean
  workspace?: string
  workspaceOptions?: WorkspaceChoice[]
  orchestrationMode?: OrchestrationMode
  onChooseWorkspace?: () => void
  onWorkspaceChange?: (value: string) => void
  onOrchestrationMode?: (value: OrchestrationMode) => void
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
  const empty = !props.messages.length && !props.busy && !props.pending.length && !props.runtimeEvents.length && !props.recoverable
  return <section className={`main-conversation-area${empty ? ' is-empty' : ''}`} aria-label="司忆会话">
    <div className="conversation-scroll">
      <div className="conversation-feed">
        {props.messages.map((message, index) => <MessageItem key={message.id || `${message.role}-${index}`} message={message} />)}
        {props.busy && <div className="thinking-line" role="status"><span /><span /><span /><p>夏目心正在处理当前任务</p></div>}
        <TaskExecutionBlock pending={props.pending} runtimeEvents={props.runtimeEvents} verification={props.verification} recoverable={props.recoverable} selectedCheckpoint={props.selectedCheckpoint} workspaceDrift={props.workspaceDrift} uncertainOperation={props.uncertainOperation} onResume={props.onResume} onAbandon={props.onAbandon} onCheckpoint={props.onCheckpoint} onApprove={props.onApprove} onReject={props.onReject} />
        <div ref={props.endRef} />
      </div>
    </div>
    <div className="conversation-compose-region">
      {empty && <div className="conversation-brand"><img src={keyLogo} alt="" /><h1>司忆</h1></div>}
      <ConversationContextBar workspace={props.workspace} workspaceOptions={props.workspaceOptions} orchestrationMode={props.orchestrationMode} onChooseWorkspace={props.onChooseWorkspace} onWorkspaceChange={props.onWorkspaceChange} onOrchestrationMode={props.onOrchestrationMode} />
      <Composer input={props.input} busy={props.busy} stopState={props.stopState} error={props.error} usage={props.usage} context={props.context} mode={props.mode} reasoningEffort={props.reasoningEffort} preferredModel={props.preferredModel} defaultModel={props.defaultModel} modelOptions={props.modelOptions} hasConversation={props.hasConversation} queuedItems={props.queuedItems} commands={props.commands} onInput={props.onInput} onMode={props.onMode} onReasoningEffort={props.onReasoningEffort} onPreferredModel={props.onPreferredModel} onSend={props.onSend} onSteer={props.onSteer} onPromoteQueued={props.onPromoteQueued} onCancelQueued={props.onCancelQueued} onStop={props.onStop} onNavigate={props.onNavigate} onUploadFile={props.onUploadFile} onClearError={props.onClearError} />
    </div>
  </section>
}
