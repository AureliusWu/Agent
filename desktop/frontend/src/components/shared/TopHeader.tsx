import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Blocks, ClipboardPaste, Copy, FolderOpen, History, KeyRound, MessageCirclePlus, Minus, PanelLeft, PanelRightOpen, Redo2, Scissors, Search, Settings, ShieldCheck, Square, TextSelect, Undo2, X } from 'lucide-react'
import type { View } from '../../types'
import { captureEditSelection, runEditAction, runWindowAction } from '../../shared/windowChrome'
import type { EditAction, EditSelection, WindowAction } from '../../shared/windowChrome'
import '../../styles/window-header.css'

interface Props {
  sidebarExpanded: boolean
  rightPanelOpen?: boolean
  onToggleSidebar: () => void
  onToggleSummary: () => void
  onNewConversation: () => void
  onNavigate: (view: View) => void
}

const applicationItems = [
  { view: 'projects', label: '项目与工作区', icon: FolderOpen },
  { view: 'search', label: '搜索', icon: Search },
  { view: 'memory', label: '记忆', icon: History },
  { view: 'models', label: '模型与 API', icon: KeyRound },
  { view: 'extensions', label: '工具与插件', icon: Blocks },
  { view: 'audit', label: '操作记录', icon: ShieldCheck },
  { view: 'settings', label: '设置与关于', icon: Settings },
] satisfies Array<{ view: View; label: string; icon: typeof FolderOpen }>

const editItems = [
  { action: 'undo', label: '撤销输入', shortcut: 'Ctrl+Z', icon: Undo2 },
  { action: 'redo', label: '重做输入', shortcut: 'Ctrl+Y', icon: Redo2 },
  { action: 'cut', label: '剪切', shortcut: 'Ctrl+X', icon: Scissors },
  { action: 'copy', label: '复制', shortcut: 'Ctrl+C', icon: Copy },
  { action: 'paste', label: '粘贴', shortcut: 'Ctrl+V', icon: ClipboardPaste },
  { action: 'selectAll', label: '全选', shortcut: 'Ctrl+A', icon: TextSelect },
] satisfies Array<{ action: EditAction; label: string; shortcut: string; icon: typeof Undo2 }>

export function TopHeader({ sidebarExpanded, rightPanelOpen = false, onToggleSidebar, onToggleSummary, onNewConversation, onNavigate }: Props) {
  const [openMenu, setOpenMenu] = useState<'application' | 'edit' | null>(null)
  const [maximized, setMaximized] = useState(false)
  const [notice, setNotice] = useState('')
  const rootRef = useRef<HTMLElement>(null)
  const appTriggerRef = useRef<HTMLButtonElement>(null)
  const editTriggerRef = useRef<HTMLButtonElement>(null)
  const editSelection = useRef<EditSelection | null>(null)
  const desktop = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window

  useEffect(() => {
    if (!desktop) return
    let disposed = false
    let unlisten: (() => void) | undefined
    void import('@tauri-apps/api/window').then(async ({ getCurrentWindow }) => {
      const current = getCurrentWindow()
      const refresh = async () => { const value = await current.isMaximized(); if (!disposed) setMaximized(value) }
      await refresh()
      const stop = await current.onResized(() => { void refresh().catch(() => {}) })
      if (disposed) stop()
      else unlisten = stop
    }).catch(() => { if (!disposed) setNotice('窗口状态暂时不可用。') })
    return () => { disposed = true; unlisten?.() }
  }, [desktop])

  useEffect(() => {
    const remember = () => {
      if (!rootRef.current?.contains(document.activeElement)) editSelection.current = captureEditSelection(document)
    }
    document.addEventListener('focusin', remember)
    document.addEventListener('selectionchange', remember)
    return () => { document.removeEventListener('focusin', remember); document.removeEventListener('selectionchange', remember) }
  }, [])

  useEffect(() => {
    if (!openMenu) return
    const frame = window.requestAnimationFrame(() => rootRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus())
    const closeOutside = (event: PointerEvent) => { if (!rootRef.current?.contains(event.target as Node)) setOpenMenu(null) }
    const escape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setOpenMenu(null)
      const trigger = openMenu === 'application' ? appTriggerRef : editTriggerRef
      trigger.current?.focus()
    }
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('keydown', escape)
    return () => { window.cancelAnimationFrame(frame); document.removeEventListener('pointerdown', closeOutside); document.removeEventListener('keydown', escape) }
  }, [openMenu])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 5000)
    return () => window.clearTimeout(timer)
  }, [notice])

  async function windowAction(action: WindowAction) {
    if (!desktop) return
    try {
      const { getCurrentWindow } = await import('@tauri-apps/api/window')
      const value = await runWindowAction(getCurrentWindow(), action)
      if (value !== null) setMaximized(value)
    } catch { setNotice('窗口操作未完成，请稍后重试。') }
  }

  function rememberSelection() {
    const selection = captureEditSelection(document)
    if (!rootRef.current?.contains(document.activeElement) || !editSelection.current) editSelection.current = selection
  }

  async function edit(action: EditAction) {
    const selection = editSelection.current || captureEditSelection(document)
    setOpenMenu(null)
    try { setNotice(await runEditAction(action, selection, document, navigator.clipboard)) }
    catch { setNotice('剪贴板操作未完成，请使用对应的键盘快捷键。') }
  }

  function menuKeys(event: KeyboardEvent<HTMLDivElement>) {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')]
    if (!items.length) return
    event.preventDefault()
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (current + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length
    items[index].focus()
  }

  const toggleMenu = (menu: 'application' | 'edit') => setOpenMenu(current => current === menu ? null : menu)

  return <header ref={rootRef} className="top-header window-header" aria-label="司忆窗口工具栏">
    <nav className="window-app-navigation" aria-label="应用菜单">
      <button type="button" className="window-toolbar-icon" onClick={onToggleSidebar} title={sidebarExpanded ? '收起侧边栏' : '打开侧边栏'} aria-label={sidebarExpanded ? '收起侧边栏' : '打开侧边栏'} aria-expanded={sidebarExpanded}><PanelLeft size={17} strokeWidth={1.7} /></button>
      <button type="button" className="window-toolbar-icon" onClick={onNewConversation} title="新建会话" aria-label="新建会话"><MessageCirclePlus size={17} strokeWidth={1.7} /></button>
      <div className="window-menu-anchor">
        <button ref={appTriggerRef} type="button" className="window-menu-trigger" aria-haspopup="menu" aria-expanded={openMenu === 'application'} aria-controls="window-application-menu" onClick={() => toggleMenu('application')} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpenMenu('application') } }}>应用</button>
        {openMenu === 'application' && <div id="window-application-menu" className="window-popup-menu" role="menu" aria-label="应用" onKeyDown={menuKeys}>
          {applicationItems.map(({ view, label, icon: Icon }) => <button type="button" role="menuitem" key={view} onClick={() => { setOpenMenu(null); onNavigate(view) }}><Icon size={16} /><span>{label}</span></button>)}
          <div className="window-menu-divider" role="separator" />
          <button type="button" role="menuitem" onClick={() => { setOpenMenu(null); onToggleSummary() }}><PanelRightOpen size={16} /><span>右侧工作台与摘要</span></button>
        </div>}
      </div>
      <div className="window-menu-anchor">
        <button ref={editTriggerRef} type="button" className="window-menu-trigger" aria-haspopup="menu" aria-expanded={openMenu === 'edit'} aria-controls="window-edit-menu" onPointerDown={rememberSelection} onClick={() => { rememberSelection(); toggleMenu('edit') }} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); rememberSelection(); setOpenMenu('edit') } }}>编辑</button>
        {openMenu === 'edit' && <div id="window-edit-menu" className="window-popup-menu" role="menu" aria-label="编辑" onKeyDown={menuKeys}>
          {editItems.map(({ action, label, shortcut, icon: Icon }) => <button type="button" role="menuitem" key={action} onClick={() => { void edit(action) }}><Icon size={16} /><span>{label}</span><kbd>{shortcut}</kbd></button>)}
        </div>}
      </div>
    </nav>
    <div className="window-drag-region" data-tauri-drag-region aria-hidden="true" />
    <button type="button" className="window-toolbar-icon window-workbench-toggle" onClick={onToggleSummary} title={rightPanelOpen ? '收起右侧工作台' : '打开右侧工作台'} aria-label={rightPanelOpen ? '收起右侧工作台' : '打开右侧工作台'} aria-expanded={rightPanelOpen} aria-controls="siyi-right-workbench"><PanelRightOpen size={17} strokeWidth={1.7} /></button>
    <div className="window-native-controls" aria-label="窗口控制">
      <button type="button" className="window-control" onClick={() => { void windowAction('minimize') }} disabled={!desktop} title="最小化" aria-label="最小化"><Minus size={15} strokeWidth={1.4} /></button>
      <button type="button" className="window-control" onClick={() => { void windowAction('maximize') }} disabled={!desktop} title={maximized ? '还原窗口' : '最大化'} aria-label={maximized ? '还原窗口' : '最大化'}>{maximized ? <Copy size={13} strokeWidth={1.4} /> : <Square size={13} strokeWidth={1.4} />}</button>
      <button type="button" className="window-control window-close" onClick={() => { void windowAction('close') }} disabled={!desktop} title="关闭" aria-label="关闭窗口"><X size={17} strokeWidth={1.4} /></button>
    </div>
    {notice && <div className="window-header-notice" role="status">{notice}</div>}
  </header>
}
