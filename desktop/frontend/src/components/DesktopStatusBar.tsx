import { RefreshCw } from 'lucide-react'
import type { DesktopBackendHealth } from '../desktopRuntime'

interface Props {
  version: string
  model: string
  busy: boolean
  health: DesktopBackendHealth | null
  restarting: boolean
  onRestart: () => void
}

const PHASE_LABEL: Record<string, string> = {
  starting: '核心启动中',
  ready: '核心在线',
  restarting: '核心恢复中',
  error: '核心异常',
  stopped: '核心已停止',
}

export function DesktopStatusBar({ version, model, busy, health, restarting, onRestart }: Props) {
  const phase = restarting ? 'restarting' : (health?.phase || 'starting')
  return <div className={`desktop-status-bar ${phase}`} role="status">
    <span>v{version}</span>
    <span title={model}>模型：{model}</span>
    <span>任务：{busy ? '执行中' : '待命'}</span>
    <span title={health?.error || health?.log_directory || ''}>{PHASE_LABEL[phase] || phase}</span>
    {(phase === 'error' || phase === 'stopped') && <button type="button" onClick={onRestart} disabled={restarting}>
      <RefreshCw size={12} />重启核心
    </button>}
  </div>
}
