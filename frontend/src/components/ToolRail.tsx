import type { ReactNode } from 'react'
import { Files, History, MessageSquare, Plug } from 'lucide-react'
import type { View } from '../types'
import '../styles/layout.css'

function RailButton({active, icon, label, onClick}: {active:boolean; icon:ReactNode; label:string; onClick:()=>void}) {
  return <button className={active ? 'active' : ''} onClick={onClick}>{icon}<span>{label}</span></button>
}

export function ToolRail({view,onView}:{view:View;onView:(view:View)=>void}) {
  return <nav className="tool-rail"><RailButton active={view==='chat'} icon={<MessageSquare/>} label="对话" onClick={()=>onView('chat')}/><RailButton active={view==='files'} icon={<Files/>} label="文件" onClick={()=>onView('files')}/><RailButton active={view==='extensions'} icon={<Plug/>} label="扩展" onClick={()=>onView('extensions')}/><RailButton active={view==='audit'} icon={<History/>} label="审计" onClick={()=>onView('audit')}/></nav>
}
