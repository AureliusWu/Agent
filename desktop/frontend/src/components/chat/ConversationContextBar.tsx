import { useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, FolderOpen, FolderPlus, Workflow } from 'lucide-react'
import { ORCHESTRATION_LABEL } from '../../constants'
import type { OrchestrationMode } from '../../types'

export interface WorkspaceChoice { label: string; value: string }

interface Props {
  workspace?: string
  workspaceOptions?: WorkspaceChoice[]
  orchestrationMode?: OrchestrationMode
  onChooseWorkspace?: () => void
  onWorkspaceChange?: (value: string) => void
  onOrchestrationMode?: (value: OrchestrationMode) => void
}

function workspaceName(path: string): string {
  return path.replace(/[\\/]+$/, '').split(/[\\/]/).at(-1) || '默认工作区'
}

export function ConversationContextBar({ workspace = '', workspaceOptions = [], orchestrationMode = 'auto', onChooseWorkspace, onWorkspaceChange, onOrchestrationMode }: Props) {
  const [open, setOpen] = useState<'workspace' | 'mode' | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const choices = [...new Map([
    ...(workspace ? [{ label: workspaceName(workspace), value: workspace }] : []),
    ...workspaceOptions,
  ].filter(item => item.value).map(item => [item.value, item])).values()]

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(null)
    }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(null) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [])

  return <div ref={root} className="conversation-context-bar" aria-label="会话工作区与模式">
    <div className="context-menu-anchor">
      <button type="button" className="context-switcher" aria-label="切换工作区" aria-expanded={open === 'workspace'} aria-haspopup="menu" title={workspace || '选择任务使用的工作区'} onClick={() => setOpen(value => value === 'workspace' ? null : 'workspace')}>
        <FolderOpen size={16} /><span>{workspaceName(workspace)}</span><ChevronDown size={12} />
      </button>
      {open === 'workspace' && <div className="context-popover workspace-popover" role="menu" aria-label="选择工作区">
        <header>工作区</header>
        <button type="button" role="menuitemradio" aria-checked={!workspace} disabled={!onWorkspaceChange} onClick={() => { onWorkspaceChange?.(''); setOpen(null) }}><span>默认工作区<small>不关联本地项目</small></span>{!workspace && <Check size={15} />}</button>
        {choices.map(item => <button type="button" role="menuitemradio" aria-checked={item.value === workspace} key={item.value} title={item.value} disabled={!onWorkspaceChange} onClick={() => { onWorkspaceChange?.(item.value); setOpen(null) }}><span>{item.label}<small>{item.value}</small></span>{item.value === workspace && <Check size={15} />}</button>)}
        <button type="button" role="menuitem" className="context-folder-action" disabled={!onChooseWorkspace} onClick={() => { setOpen(null); onChooseWorkspace?.() }}><FolderPlus size={16} />选择本地文件夹…</button>
      </div>}
    </div>
    <div className="context-menu-anchor">
      <button type="button" className="context-switcher" aria-label="切换任务模式" aria-expanded={open === 'mode'} aria-haspopup="menu" onClick={() => setOpen(value => value === 'mode' ? null : 'mode')}>
        <Workflow size={16} /><span>{orchestrationMode === 'single' ? '标准模式' : ORCHESTRATION_LABEL[orchestrationMode]}</span><ChevronDown size={12} />
      </button>
      {open === 'mode' && <div className="context-popover mode-popover" role="menu" aria-label="选择任务模式">
        <header>任务模式</header>
        {(Object.entries(ORCHESTRATION_LABEL) as [OrchestrationMode, string][]).map(([value, label]) => <button type="button" key={value} role="menuitemradio" aria-checked={value === orchestrationMode} disabled={!onOrchestrationMode} onClick={() => { onOrchestrationMode?.(value); setOpen(null) }}><span>{value === 'single' ? '标准模式' : label}{value === 'single' && <small>单 Agent 直接处理任务</small>}{value === 'auto' && <small>根据任务自动调度</small>}</span>{value === orchestrationMode && <Check size={15} />}</button>)}
      </div>}
    </div>
  </div>
}
