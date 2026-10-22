import { MessageSquare, MoreHorizontal, Pencil, Trash2 } from 'lucide-react'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { Conversation } from '../../types'

interface Props {
  conversations: Conversation[]
  active: Conversation | null
  onSelect: (item: Conversation) => void
  onRename: (item: Conversation) => void
  onDelete: (item: Conversation) => void
}

function conversationTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const today = new Date()
  if (date.toDateString() === today.toDateString()) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  }
  return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }).replace('/', '-')
}

export function RecentConversationList({ conversations, active, onSelect, onRename, onDelete }: Props) {
  const [openMenu, setOpenMenu] = useState<{ conversationId: number; anchor: HTMLButtonElement } | null>(null)
  const [position, setPosition] = useState<{ left: number; top: number; maxHeight: number } | null>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuConversation = conversations.find(item => item.id === openMenu?.conversationId)

  function closeMenu(restoreFocus = false) {
    setOpenMenu(null)
    setPosition(null)
    if (restoreFocus && openMenu?.anchor.isConnected) openMenu.anchor.focus()
  }

  useEffect(() => { setOpenMenu(null); setPosition(null) }, [active?.id])

  useEffect(() => {
    if (openMenu && (!menuConversation || !openMenu.anchor.isConnected)) {
      setOpenMenu(null)
      setPosition(null)
    }
  }, [openMenu, menuConversation])

  useLayoutEffect(() => {
    if (!openMenu || !menuRef.current) return
    const anchor = openMenu.anchor.getBoundingClientRect()
    const menu = menuRef.current.getBoundingClientRect()
    const maxHeight = Math.max(0, window.innerHeight - 16)
    const height = Math.min(menu.height, maxHeight)
    const below = anchor.bottom + 3
    const top = below + height <= window.innerHeight - 8 ? below : anchor.top - height - 3
    setPosition({
      left: Math.max(8, Math.min(anchor.right - menu.width, window.innerWidth - menu.width - 8)),
      top: Math.max(8, Math.min(top, window.innerHeight - height - 8)),
      maxHeight,
    })
  }, [openMenu])

  useLayoutEffect(() => {
    if (openMenu && position) menuRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
  }, [openMenu, position])

  useEffect(() => {
    if (!openMenu) return
    const outside = (event: PointerEvent | FocusEvent) => {
      if (!(event.target instanceof Node) || (!openMenu.anchor.contains(event.target) && !menuRef.current?.contains(event.target))) {
        setOpenMenu(null)
        setPosition(null)
      }
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopPropagation()
      setOpenMenu(null)
      setPosition(null)
      if (openMenu.anchor.isConnected) openMenu.anchor.focus()
    }
    const dismiss = () => { setOpenMenu(null); setPosition(null) }
    const scroll = (event: Event) => {
      if (!(event.target instanceof Node) || !menuRef.current?.contains(event.target)) dismiss()
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    document.addEventListener('keydown', escape)
    window.addEventListener('scroll', scroll, true)
    window.addEventListener('resize', dismiss)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('focusin', outside)
      document.removeEventListener('keydown', escape)
      window.removeEventListener('scroll', scroll, true)
      window.removeEventListener('resize', dismiss)
    }
  }, [openMenu])

  function runMenuAction(action: (item: Conversation) => void) {
    if (!menuConversation) return
    closeMenu(true)
    action(menuConversation)
  }

  return <div className="recent-conversations">
    <h2>最近对话</h2>
    <div className="recent-list">
      {conversations.length === 0 && <p className="sidebar-empty">暂无对话</p>}
      {conversations.map(item => <div className={active?.id === item.id ? 'recent-item active' : 'recent-item'} key={item.id}>
        <button type="button" className="recent-main" onClick={() => { closeMenu(); onSelect(item) }}>
          <MessageSquare size={16} />
          <span>{item.title}</span>
          <time>{conversationTime(item.updated_at)}</time>
        </button>
        <button type="button" id={`recent-more-${item.id}`} className="recent-more" aria-label={`${item.title}的更多操作`} aria-expanded={openMenu?.conversationId === item.id} aria-controls={openMenu?.conversationId === item.id ? `recent-menu-${item.id}` : undefined} onClick={event => {
          setPosition(null)
          setOpenMenu(openMenu?.conversationId === item.id ? null : { conversationId: item.id, anchor: event.currentTarget })
        }}><MoreHorizontal size={16} /></button>
      </div>)}
    </div>
    {openMenu && menuConversation && createPortal(<div ref={menuRef} id={`recent-menu-${menuConversation.id}`} className="recent-menu" role="group" aria-label={`${menuConversation.title}的操作`} style={{ position: 'fixed', left: position?.left ?? 0, top: position?.top ?? 0, maxHeight: position?.maxHeight, visibility: position ? 'visible' : 'hidden' }}>
      <button type="button" onClick={() => runMenuAction(onRename)}><Pencil size={14} />重命名</button>
      <button type="button" onClick={() => runMenuAction(onDelete)}><Trash2 size={14} />删除</button>
    </div>, document.body)}
  </div>
}
