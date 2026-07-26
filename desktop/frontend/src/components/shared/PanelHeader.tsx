import type { ReactNode } from 'react'

export function PanelHeader({icon, title, subtitle}: {icon:ReactNode; title:string; subtitle:string}) {
  return <header className="panel-header"><span>{icon}</span><div><h2>{title}</h2><p>{subtitle}</p></div></header>
}
