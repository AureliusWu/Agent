import { Menu, PanelRightOpen } from 'lucide-react'
import keyLogo from '../../assets/kokoro-key.svg'

interface Props {
  sidebarExpanded: boolean
  onToggleSidebar: () => void
  onToggleSummary: () => void
}

export function TopHeader({ sidebarExpanded, onToggleSidebar, onToggleSummary }: Props) {
  return <header className="top-header">
    <button className="line-icon-button" onClick={onToggleSidebar} aria-label={sidebarExpanded ? '收起左侧栏' : '展开左侧栏'} aria-expanded={sidebarExpanded}>
      <Menu size={21} />
    </button>
    <img className="brand-key" src={keyLogo} alt="" />
    <strong className="brand-name">司忆</strong>
    <button className="line-icon-button summary-toggle" onClick={onToggleSummary} aria-label="打开夏目心与摘要">
      <PanelRightOpen size={20} />
    </button>
  </header>
}
