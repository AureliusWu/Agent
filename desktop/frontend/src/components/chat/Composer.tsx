import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowUp, ChevronDown, LockKeyhole, Plus, Send, Sparkles, Square, X } from 'lucide-react'
import { MODE_LABEL, REASONING_LABEL } from '../../constants'
import { commandDisabledReason, completedCommandText, matchingCommands, moveCommandIndex } from '../../commands/commandMenu'
import type { CommandDefinition, ContextStats, ConversationQueueItem, PermissionMode, ReasoningEffort, TokenUsage, View } from '../../types'
import type { ReasoningProvider } from '../../shared/reasoningSlider'
import { reasoningExplicitlyUnsupported } from '../../shared/reasoningSlider'
import { AttachmentMenu } from './AttachmentMenu'
import { ModelReasoningMenu } from '../tasks/ModelReasoningMenu'
import { PermissionMenu } from '../settings/PermissionMenu'
import type { StopState } from '../../hooks/useAgentChat'
import { VoiceInputControl } from './VoiceInputControl'
import { useVoiceChat } from './VoiceChatContext'

type OpenMenu = 'attachments' | 'permission' | 'model' | null

interface Props {
  input: string
  busy: boolean
  stopState: StopState
  error: string
  usage: TokenUsage | null
  context: ContextStats | null
  mode: PermissionMode
  reasoningEffort: ReasoningEffort
  reasoningProvider?: ReasoningProvider
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  hasConversation: boolean
  queuedItems: ConversationQueueItem[]
  commands: CommandDefinition[]
  onInput: (value: string) => void
  onMode: (mode: PermissionMode) => void | Promise<void>
  onReasoningEffort: (value: ReasoningEffort) => void
  onPreferredModel: (value: string) => void
  onSend: () => void
  onSteer: () => void
  onPromoteQueued: (itemId: string) => void | Promise<void>
  onCancelQueued: (itemId: string) => void | Promise<void>
  onStop: () => void
  onNavigate: (view: View) => void
  onUploadFile: (file: File) => void | Promise<void>
  onClearError: () => void
}

export function Composer(props: Props) {
  const voice = useVoiceChat()
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null)
  const [commandIndex, setCommandIndex] = useState(0)
  const [commandMenuDismissed, setCommandMenuDismissed] = useState(false)
  const [commandPickerOpen, setCommandPickerOpen] = useState(false)
  const rootRef = useRef<HTMLFormElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const modelName = props.preferredModel || props.defaultModel || '自动路由'
  const displayedReasoning = reasoningExplicitlyUnsupported(props.reasoningProvider) ? 'auto' : props.reasoningEffort
  const modelSummary = `${modelName} · ${REASONING_LABEL[displayedReasoning]} · 标准速度`
  const commandMatches = useMemo(
    () => matchingCommands(commandPickerOpen ? '/' : props.input, props.commands).filter(command =>
      (!command.requires_conversation || props.hasConversation) && !command.requires_workspace,
    ),
    [commandPickerOpen, props.commands, props.hasConversation, props.input],
  )
  const commandPrefixOpen = props.input.trimStart().startsWith('/') && !props.input.trimStart().includes(' ')
  const showCommands = !commandMenuDismissed && (commandPickerOpen || commandPrefixOpen)

  useEffect(() => { setCommandIndex(0) }, [props.input])
  useEffect(() => { if (props.busy) setOpenMenu(current => current === 'model' ? null : current) }, [props.busy])

  function commandDisabled(command: CommandDefinition): string {
    const reason = commandDisabledReason(command, props)
    if (reason) return reason
    return commandPickerOpen && !command.accepts_arguments && Boolean(props.input.trim()) && !props.input.trimStart().startsWith('/') ? '请先保留或清空已有草稿' : ''
  }

  function completeCommand(command: CommandDefinition) {
    if (commandDisabled(command)) return
    const draft = commandPickerOpen && command.accepts_arguments && !props.input.trimStart().startsWith('/') ? props.input : ''
    props.onInput(completedCommandText(command) + draft)
    setCommandPickerOpen(false)
    setCommandMenuDismissed(true)
  }

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) { setOpenMenu(null); setCommandPickerOpen(false) }
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpenMenu(null) }
    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape) }
  }, [])

  const toggleMenu = (menu: Exclude<OpenMenu, null>) => setOpenMenu(value => value === menu ? null : menu)

  return <form ref={rootRef} className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); props.onSend() }}>
    {props.error && <div className="composer-error" role="alert"><span>{props.error}</span><button type="button" onClick={props.onClearError} aria-label="关闭错误"><X size={15} /></button></div>}
    <textarea ref={inputRef} data-composer-input aria-label="输入消息" value={props.input} onChange={event => { props.onInput(event.target.value); setCommandMenuDismissed(false); setCommandPickerOpen(false) }} placeholder="描述你的任务，输入 / 调用指令" rows={3} onKeyDown={event => {
      if (event.nativeEvent.isComposing) return
      if (showCommands && commandMatches.length && event.key === 'ArrowDown') { event.preventDefault(); setCommandIndex(value => moveCommandIndex(value, commandMatches.length, 1)); return }
      if (showCommands && commandMatches.length && event.key === 'ArrowUp') { event.preventDefault(); setCommandIndex(value => moveCommandIndex(value, commandMatches.length, -1)); return }
      if (showCommands && event.key === 'Escape') { event.preventDefault(); setCommandMenuDismissed(true); setCommandPickerOpen(false); return }
      if (showCommands && commandMatches.length && (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey && (commandPickerOpen || !props.input.trim().includes(' '))))) {
        event.preventDefault(); completeCommand(commandMatches[commandIndex] || commandMatches[0]); return
      }
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); props.onSend() }
    }} />
    {showCommands && <div className="command-menu" role="listbox" aria-label="本地斜杠指令">
      {!commandMatches.length && <p className="command-menu-empty">没有可用指令</p>}
      {commandMatches.map((command, index) => {
        const disabled = commandDisabled(command)
        return <button type="button" role="option" aria-selected={index === commandIndex} disabled={Boolean(disabled)} className={index === commandIndex ? 'active' : ''} key={command.name} onMouseEnter={() => setCommandIndex(index)} onClick={() => completeCommand(command)}>
          <span><strong>{command.usage}</strong><small>{command.description}</small></span>
          {disabled && <em>{disabled}</em>}
        </button>
      })}
    </div>}
    {props.usage && <div className="token-meter" title={`输入 ${props.usage.input_tokens.toLocaleString()} · 输出 ${props.usage.output_tokens.toLocaleString()}`}><small>累计 {props.usage.total_tokens.toLocaleString()} Token{props.context ? ` · 当前上下文约 ${props.context.estimated_tokens.toLocaleString()} · 已压缩至 #${props.context.compacted_through}` : ''}</small></div>}
    {props.queuedItems.length > 0 && <div className="composer-queue-status" role="status">
      <span>队列中 {props.queuedItems.length} 项</span>
      <div className="composer-queue-items">
        {props.queuedItems.slice(0, 3).map(item => <div key={item.id} className="composer-queue-item">
          <span title={item.content}>{item.content}</span>
          {item.priority !== 'next' && <button type="button" onClick={() => void props.onPromoteQueued(item.id)} title="设为下一项" aria-label="设为下一项"><ArrowUp size={13} /></button>}
          <button type="button" onClick={() => void props.onCancelQueued(item.id)} title="取消排队" aria-label="取消排队"><X size={13} /></button>
        </div>)}
      </div>
    </div>}
    <div className="composer-toolbar">
      <div className="composer-menu-anchor">
        <button type="button" className={openMenu === 'attachments' ? 'composer-icon active' : 'composer-icon'} onClick={() => toggleMenu('attachments')} aria-label="添加文件或调用指令" aria-expanded={openMenu === 'attachments'}><Plus size={20} /></button>
        {openMenu === 'attachments' && <AttachmentMenu hasConversation={props.hasConversation} onUpload={() => fileRef.current?.click()} onCommand={() => { setCommandPickerOpen(true); setCommandIndex(0); setCommandMenuDismissed(false); inputRef.current?.focus() }} onNavigate={props.onNavigate} onClose={() => setOpenMenu(null)} />}
        <input ref={fileRef} type="file" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void props.onUploadFile(file) }} />
      </div>

      <div className="composer-menu-anchor permission-anchor">
        <button type="button" className={openMenu === 'permission' ? 'composer-control active' : 'composer-control'} onClick={() => toggleMenu('permission')} aria-label="选择权限" aria-expanded={openMenu === 'permission'}><LockKeyhole size={15} /><span>{MODE_LABEL[props.mode]}</span><ChevronDown size={12} /></button>
        {openMenu === 'permission' && <PermissionMenu mode={props.mode} onSelect={async mode => { await props.onMode(mode); setOpenMenu(null) }} />}
      </div>

      <div className="composer-menu-anchor model-anchor">
        <button type="button" className={openMenu === 'model' ? 'composer-control model-trigger active' : 'composer-control model-trigger'} disabled={props.busy} onClick={() => { if (!props.busy) toggleMenu('model') }} aria-label="切换模型与推理" aria-expanded={!props.busy && openMenu === 'model'} title={props.busy ? '任务执行中，完成后可切换模型与推理' : modelSummary}><span>{modelName}</span><small>{REASONING_LABEL[displayedReasoning]}</small><ChevronDown size={12} /></button>
        {!props.busy && openMenu === 'model' && <ModelReasoningMenu preferredModel={props.preferredModel} defaultModel={props.defaultModel} modelOptions={props.modelOptions} reasoningEffort={props.reasoningEffort} reasoningProvider={props.reasoningProvider} busy={props.busy} onPreferredModel={props.onPreferredModel} onReasoningEffort={props.onReasoningEffort} />}
      </div>

      {voice && <VoiceInputControl conversationId={voice.conversationId} boundVoiceSessionId={voice.boundVoiceSessionId} onInterruptTts={voice.onInterruptTts} onTranscript={voice.onTranscript} onDiscardTranscript={voice.onDiscardTranscript} />}

      {props.busy ? <div className="composer-run-controls">
        <button type="submit" disabled={!props.input.trim()}><Send size={15} />排队</button>
        <button type="button" disabled={!props.input.trim()} onClick={props.onSteer}><Sparkles size={15} />引导</button>
        <button type="button" disabled={props.stopState === 'stopping'} onClick={props.onStop}>
          <Square size={14} />{props.stopState === 'stopping' ? '正在停止' : props.stopState === 'failed' ? '停止失败' : '停止'}
        </button>
      </div> : <button className="send-button" disabled={!props.input.trim()} aria-label="发送" title="发送 · Enter"><ArrowUp size={20} /><span className="composer-sr-only">发送</span></button>}
    </div>
  </form>
}
