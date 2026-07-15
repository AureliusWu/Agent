import type { ReactNode } from 'react'
import { Files, History, MessageSquare, Plug, Settings, Waves } from 'lucide-react'
import type { View } from '../types'
import '../styles/layout.css'

function RailButton({ active, icon, label, onClick }: { active: boolean; icon: ReactNode; label: string; onClick: () => void }) {
  return <button className={active ? 'active' : ''} onClick={onClick}>{icon}<span>{label}</span></button>
}

export function ToolRail({ view, onView }: { view: View; onView: (view: View) => void }) {
  return <nav className="tool-rail" aria-label="主导航">
    <RailButton active={view === 'chat'} icon={<MessageSquare />} label="对话" onClick={() => onView('chat')} />
    <RailButton active={view === 'audit'} icon={<History />} label="任务" onClick={() => onView('audit')} />
    <RailButton active={view === 'memory'} icon={<Waves />} label="记忆" onClick={() => onView('memory')} />
    <RailButton active={view === 'files'} icon={<Files />} label="文件" onClick={() => onView('files')} />
    <RailButton active={view === 'extensions'} icon={<Plug />} label="能力" onClick={() => onView('extensions')} />
    <RailButton active={view === 'settings'} icon={<Settings />} label="设置" onClick={() => onView('settings')} />
  </nav>
}
