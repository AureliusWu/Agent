import { useRef } from 'react'
import type { PointerEvent } from 'react'
import { Mic, Radio, Square, X } from 'lucide-react'
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

  function toggle() {
    if (canStart) void capture.start()
    else if (canStop) capture.stop()
  }

  function beginPushToTalk(event: PointerEvent<HTMLButtonElement>) {
    if (!canStart) return
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

  return <div className={`voice-input-control ${capture.state}`} aria-live="polite">
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
    <div className="voice-status">
      <span>{stateLabel(capture.state)}</span>
      {capture.state === 'recording' && <time>{durationLabel(capture.elapsedMs)}</time>}
    </div>
    <span className="voice-permission" title={`麦克风权限：${microphonePermissionLabel(capture.permission)}`}>{microphonePermissionLabel(capture.permission)}</span>
    <span className="voice-level" aria-label={`输入音量 ${levelPercent}%`}><i style={{ width: `${levelPercent}%` }} /></span>
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
    <label className="voice-auto-send" title="转写完成后直接进入普通任务队列">
      <input type="checkbox" checked={capture.autoSend} disabled={!canStart} onChange={event => capture.setAutoSend(event.target.checked)} /> 自动发送
    </label>
    {canCancel && <button type="button" className="voice-cancel" onClick={() => void capture.cancel()} aria-label="取消语音输入" title="取消语音输入"><X size={14} /></button>}
    {capture.error && <p className="voice-input-error" role="alert">{capture.error}</p>}
  </div>
}
