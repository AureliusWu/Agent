import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { ChevronDown, Mic, Radio, Square, X } from 'lucide-react'
import { useVoiceCapture, type VoiceTranscriptInput } from '../../hooks/useVoiceCapture'
import { microphoneLevelPercent, microphonePermissionLabel, WINDOWS_DEFAULT_MICROPHONE_LABEL } from '../../microphoneUiPolicy'

interface Props {
  conversationId: number | null
  boundVoiceSessionId?: string | null
  onInterruptTts: () => Promise<void> | void
  onTranscript: (input: VoiceTranscriptInput) => Promise<void> | void
  onDiscardTranscript: (voiceSessionId: string) => void
}

function stateLabel(state: ReturnType<typeof useVoiceCapture>['state']): string {
  switch (state) {
    case 'requesting_permission': return '正在请求麦克风权限'
    case 'recording': return '正在录音'
    case 'processing': return '正在处理音频'
    case 'transcribing': return '正在本地转写'
    case 'reviewing': return '已转写，可编辑后发送'
    case 'error': return '语音输入未完成'
    default: return '准备就绪'
  }
}

function durationLabel(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export function VoiceInputControl(props: Props) {
  const pttPointerIdRef = useRef<number | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const capture = useVoiceCapture({
    conversationId: props.conversationId,
    boundVoiceSessionId: props.boundVoiceSessionId,
    onInterruptTts: props.onInterruptTts,
    onTranscript: props.onTranscript,
    onDiscardTranscript: props.onDiscardTranscript,
  })
  const canStart = Boolean(props.conversationId) && (capture.state === 'idle' || capture.state === 'error')
  const canStop = capture.state === 'recording'
  const canCancel = capture.state !== 'idle'
  const levelPercent = microphoneLevelPercent(capture.level)

  useEffect(() => {
    const outside = (event: Event) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setDetailsOpen(false)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setDetailsOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [])

  function toggle() {
    setDetailsOpen(true)
    if (canStart) void capture.start()
    else if (canStop) capture.stop()
  }

  function beginPushToTalk(event: PointerEvent<HTMLButtonElement>) {
    if (!canStart) return
    setDetailsOpen(true)
    event.preventDefault()
    pttPointerIdRef.current = event.pointerId
    event.currentTarget.setPointerCapture(event.pointerId)
    void capture.start()
  }

  function endPushToTalk(event: PointerEvent<HTMLButtonElement>) {
    if (pttPointerIdRef.current !== event.pointerId) return
    pttPointerIdRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    capture.stop()
  }

  return <div ref={rootRef} className={`voice-input-control ${capture.state}`} aria-live="polite">
    <button
      type="button"
      className="voice-toggle"
      disabled={!canStart && !canStop}
      onClick={toggle}
      aria-label={canStop ? '停止录音' : '开始录音'}
      aria-pressed={canStop}
      title={canStop ? '停止录音' : '点击开始录音'}
    >
      {canStop ? <Square size={15} /> : <Mic size={17} />}
    </button>
    <button type="button" className="voice-options" onClick={() => setDetailsOpen(value => !value)} aria-label="语音输入设置" aria-haspopup="dialog" aria-expanded={detailsOpen} title="语音输入设置"><ChevronDown size={11} /></button>
    {capture.state === 'recording' && <span className="voice-recording-time" aria-label="录音时长">{durationLabel(capture.elapsedMs)}</span>}
    {detailsOpen && <div className="voice-details" role="dialog" aria-label="语音输入设置与状态">
    <header><strong>语音输入</strong><button type="button" onClick={() => setDetailsOpen(false)} aria-label="收起语音设置"><X size={15} /></button></header>
    <div className="voice-status-row">
      <div className="voice-status"><span>{stateLabel(capture.state)}</span>{capture.state === 'recording' && <time>{durationLabel(capture.elapsedMs)}</time>}</div>
      <span className="voice-permission" title={`麦克风权限：${microphonePermissionLabel(capture.permission)}`}>{microphonePermissionLabel(capture.permission)}</span>
    </div>
    <div className="voice-level-row"><span>输入音量 {levelPercent}%</span><span className="voice-level" aria-label={`输入音量 ${levelPercent}%`}><i style={{ width: `${levelPercent}%` }} /></span></div>
    <button
      type="button"
      className="voice-ptt"
      disabled={!canStart}
      onPointerDown={beginPushToTalk}
      onPointerUp={endPushToTalk}
      onPointerCancel={endPushToTalk}
      aria-label="按住说话，松开结束"
      title="按住说话，松开结束"
    >
      <Radio size={14} /><span>按住说话</span>
    </button>
    <label className="voice-field-label">麦克风设备
    <select
      className="voice-device"
      value={capture.selectedDeviceId}
      disabled={!canStart}
      onChange={event => capture.setSelectedDeviceId(event.target.value)}
      aria-label="选择麦克风"
      title="选择麦克风"
    >
      <option value="">{WINDOWS_DEFAULT_MICROPHONE_LABEL}</option>
      {capture.devices.map(device => <option key={device.id} value={device.id}>{device.label}</option>)}
    </select>
    </label>
    <label className="voice-field-label">按住说话快捷键
    <select
      className="voice-shortcut"
      value={capture.shortcut}
      disabled={!canStart}
      onChange={event => capture.setShortcut(event.target.value)}
      aria-label="按住说话快捷键"
      title="应用内按住说话快捷键"
    >
      <option value="Space">快捷键：空格</option>
      <option value="Enter">快捷键：回车</option>
      <option value="Alt+R">快捷键：Alt+R</option>
    </select>
    </label>
    <label className="voice-auto-send" title="转写完成后直接进入普通任务队列">
      <input type="checkbox" checked={capture.autoSend} disabled={!canStart} onChange={event => capture.setAutoSend(event.target.checked)} /> 自动发送
    </label>
    {canCancel && <button type="button" className="voice-cancel" onClick={() => void capture.cancel()} aria-label="取消语音输入" title="取消语音输入"><X size={14} /><span>取消语音输入</span></button>}
    {capture.error && <p className="voice-details-error" role="alert">{capture.error}</p>}
    </div>}
    {capture.error && !detailsOpen && <p className="voice-input-error" role="alert">{capture.error}</p>}
  </div>
}
