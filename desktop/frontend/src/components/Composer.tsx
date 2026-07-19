import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ArrowUp, LockKeyhole, Plus, Send, Sparkles, Square, X } from 'lucide-react'
import { MODE_LABEL, REASONING_LABEL } from '../constants'
import type { ContextStats, ConversationQueueItem, PermissionMode, ReasoningEffort, TokenUsage, View } from '../types'
import { AttachmentMenu } from './AttachmentMenu'
import { ModelReasoningMenu } from './ModelReasoningMenu'
import { PermissionMenu } from './PermissionMenu'

type OpenMenu = 'attachments' | 'permission' | 'model' | null

interface Props {
  input: string
  busy: boolean
  error: string
  usage: TokenUsage | null
  context: ContextStats | null
  mode: PermissionMode
  reasoningEffort: ReasoningEffort
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  hasConversation: boolean
  queuedItems: ConversationQueueItem[]
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
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null)
  const rootRef = useRef<HTMLFormElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const modelName = props.preferredModel || props.defaultModel || '自动路由'
  const modelSummary = `${modelName} · ${REASONING_LABEL[props.reasoningEffort]} · 标准速度`

  useEffect(() => {
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpenMenu(null)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpenMenu(null) }
    document.addEventListener('pointerdown', close)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', close); document.removeEventListener('keydown', escape) }
  }, [])

  const toggleMenu = (menu: Exclude<OpenMenu, null>) => setOpenMenu(value => value === menu ? null : menu)

  return <form ref={rootRef} className="composer" onSubmit={(event: FormEvent) => { event.preventDefault(); props.onSend() }}>
    {props.error && <div className="composer-error" role="alert"><span>{props.error}</span><button type="button" onClick={props.onClearError} aria-label="关闭错误"><X size={15} /></button></div>}
    <textarea value={props.input} onChange={event => props.onInput(event.target.value)} placeholder="输入消息，或描述你的任务…" rows={3} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); props.onSend() } }} />
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
        <button type="button" className={openMenu === 'attachments' ? 'composer-icon active' : 'composer-icon'} onClick={() => toggleMenu('attachments')} aria-label="添加文件或打开更多能力" aria-expanded={openMenu === 'attachments'}><Plus size={20} /></button>
        {openMenu === 'attachments' && <AttachmentMenu hasConversation={props.hasConversation} onUpload={() => fileRef.current?.click()} onNavigate={props.onNavigate} onClose={() => setOpenMenu(null)} />}
        <input ref={fileRef} type="file" hidden onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void props.onUploadFile(file) }} />
      </div>

      <div className="composer-menu-anchor permission-anchor">
        <button type="button" className={openMenu === 'permission' ? 'composer-control active' : 'composer-control'} onClick={() => toggleMenu('permission')} aria-expanded={openMenu === 'permission'}><LockKeyhole size={16} /><span>{MODE_LABEL[props.mode]}</span></button>
        {openMenu === 'permission' && <PermissionMenu mode={props.mode} onSelect={async mode => { await props.onMode(mode); setOpenMenu(null) }} />}
      </div>

      <div className="composer-menu-anchor model-anchor">
        <button type="button" className={openMenu === 'model' ? 'composer-control model-trigger active' : 'composer-control model-trigger'} onClick={() => toggleMenu('model')} aria-expanded={openMenu === 'model'} title={modelSummary}><Sparkles size={16} /><span>{modelSummary}</span></button>
        {openMenu === 'model' && <ModelReasoningMenu preferredModel={props.preferredModel} defaultModel={props.defaultModel} modelOptions={props.modelOptions} reasoningEffort={props.reasoningEffort} onPreferredModel={props.onPreferredModel} onReasoningEffort={props.onReasoningEffort} />}
      </div>

      {props.busy ? <div className="composer-run-controls">
        <button type="submit" disabled={!props.input.trim()}><Send size={15} />排队</button>
        <button type="button" disabled={!props.input.trim()} onClick={props.onSteer}><Sparkles size={15} />引导</button>
        <button type="button" onClick={props.onStop}><Square size={14} />停止</button>
      </div> : <button className="send-button" disabled={!props.input.trim()} aria-label="发送"><Send size={17} /><span>发送</span></button>}
    </div>
  </form>
}
