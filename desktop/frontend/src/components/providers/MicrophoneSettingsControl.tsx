import { useState } from 'react'
import { Mic, Square, X } from 'lucide-react'
import { useVoiceCapture } from '../../hooks/useVoiceCapture'
import {
  microphoneLevelPercent,
  microphonePermissionLabel,
  WINDOWS_DEFAULT_MICROPHONE_LABEL,
} from '../../microphoneUiPolicy'
import { useVoiceChat } from '../chat/VoiceChatContext'

interface Props {
  provider: string
  model: string
  device: string
  computeType: string
}

function captureStatus(state: ReturnType<typeof useVoiceCapture>['state']): string {
  if (state === 'requesting_permission') return '正在请求权限'
  if (state === 'recording') return '正在录音'
  if (state === 'processing') return '正在处理录音'
  if (state === 'transcribing') return '正在测试转写'
  if (state === 'reviewing') return '测试转写已完成'
  if (state === 'error') return '测试未完成'
  return '准备就绪'
}

/**
 * Settings-page microphone test. It deliberately reuses useVoiceCapture so
 * permission, capture ownership, WAV normalization, local STT, cancellation
 * and cleanup stay identical to the production composer path. testMode keeps
 * the transcript local to this panel and never submits an ordinary message.
 */
export function MicrophoneSettingsControl({ provider, model, device, computeType }: Props) {
  const voice = useVoiceChat()
  const [testTranscript, setTestTranscript] = useState('')
  const capture = useVoiceCapture({
    conversationId: voice?.conversationId || null,
    testMode: true,
    onInterruptTts: voice?.onInterruptTts || (() => { window.dispatchEvent(new CustomEvent('siyi:tts-interrupt')) }),
    onTranscript: input => setTestTranscript(input.text),
    onDiscardTranscript: () => undefined,
  })
  const levelPercent = microphoneLevelPercent(capture.level)
  const selectedDevice = capture.selectedDeviceId
    ? capture.devices.find(item => item.id === capture.selectedDeviceId)?.label || '已保存的麦克风（当前不可用）'
    : WINDOWS_DEFAULT_MICROPHONE_LABEL
  const canStart = Boolean(voice?.conversationId) && (capture.state === 'idle' || capture.state === 'error')
  const canTranscribe = capture.state === 'recording'
  const canCancel = capture.state !== 'idle'

  return <div className="local-ai-controls microphone-settings-control" data-testid="microphone-settings-control">
    <p><strong>当前 STT Provider：</strong>{provider || '未配置'}</p>
    <p><strong>当前模型：</strong>{model || '未配置'} · {device || 'cpu'} / {computeType || 'int8'}</p>
    <p><strong>麦克风权限：</strong>{microphonePermissionLabel(capture.permission)}（{capture.permission}）</p>
    <label>
      当前设备
      <select
        value={capture.selectedDeviceId}
        disabled={!canStart}
        onChange={event => capture.setSelectedDeviceId(event.target.value)}
        aria-label="选择测试麦克风"
      >
        <option value="">{WINDOWS_DEFAULT_MICROPHONE_LABEL}</option>
        {capture.devices.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select>
    </label>
    <p><strong>实际选择：</strong>{selectedDevice}</p>
    <div className="microphone-test-level">
      <span>输入电平 {levelPercent}%</span>
      <span className="voice-level" aria-label={`测试输入音量 ${levelPercent}%`}>
        <i style={{ width: `${levelPercent}%` }} />
      </span>
    </div>
    <p><strong>测试状态：</strong>{captureStatus(capture.state)}</p>
    <div className="microphone-test-actions">
      <button type="button" className="secondary" disabled={!canStart} onClick={() => { setTestTranscript(''); void capture.start() }}>
        <Mic size={15} />测试录音
      </button>
      <button type="button" className="secondary" disabled={!canTranscribe} onClick={capture.stop}>
        <Square size={15} />停止并测试转写
      </button>
      {canCancel && <button type="button" className="secondary" onClick={() => void capture.cancel()}>
        <X size={15} />取消测试
      </button>}
    </div>
    {!voice?.conversationId && <p className="extension-error">请先新建或选择一个对话，以绑定临时语音会话；测试结果不会作为消息发送。</p>}
    {testTranscript && <output className="microphone-test-transcript"><strong>测试转写结果：</strong>{testTranscript}</output>}
    {capture.error && <p className="extension-error" role="alert">{capture.error}</p>}
  </div>
}
