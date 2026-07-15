import { Gauge, Menu } from 'lucide-react'
import { MODE_LABEL } from '../constants'
import type { ContextStats, Conversation, PermissionMode } from '../types'
import '../styles/layout.css'

export function Topbar({ active, mode, context, onMenu, onMode, onCompact }: { active: Conversation | null; mode: PermissionMode; context: ContextStats | null; onMenu: () => void; onMode: (mode: PermissionMode) => void; onCompact: () => void }) {
  return <header className="topbar">
    <button className="icon-btn mobile-only" onClick={onMenu} aria-label="菜单"><Menu size={20} /></button>
    <div className="topbar-title">
      <span className="topbar-eyebrow">KOKORO INTERFACE</span>
      <h1>{active?.title || '记忆海终端'}</h1>
      <p>{active ? active.workspace : '夏目心等待管理员创建第一段对话'}</p>
    </div>
    {active && <button className="context-meter" onClick={onCompact} title="压缩长对话上下文"><Gauge size={15} /><span>{context ? `约 ${context.estimated_tokens.toLocaleString()} tokens` : '上下文'}</span></button>}
    <div className="permission-switch" aria-label="权限模式">
      {(['ask', 'agent', 'full'] as PermissionMode[]).map(item => <button key={item} className={mode === item ? 'active' : ''} onClick={() => onMode(item)}>{MODE_LABEL[item]}</button>)}
    </div>
  </header>
}
