import { Files, History, Info, MoreHorizontal, Plug, Waves, X } from 'lucide-react'
import { useState } from 'react'
import { CHARACTER_ASSETS } from '../characterAssets'
import type { ContextStats, VerificationReport, View } from '../types'
import { CharacterCard } from './CharacterCard'
import { ConversationSummary } from './ConversationSummary'

interface Props {
  open: boolean
  hasConversation: boolean
  context: ContextStats | null
  busy: boolean
  pendingCount: number
  recoverable: boolean
  verification: VerificationReport | null
  onClose: () => void
  onNavigate: (view: View) => void
  onCompact: () => void
}

export function KokoroPanel(props: Props) {
  const [menuOpen, setMenuOpen] = useState(false)
  const navigate = (view: View) => { props.onNavigate(view); setMenuOpen(false); props.onClose() }
  return <aside className={props.open ? 'kokoro-panel open' : 'kokoro-panel'} aria-label="夏目心与对话摘要">
    <CharacterCard {...CHARACTER_ASSETS.natsumeKokoro} status={props.busy ? 'busy' : 'idle'} actions={<>
        <div className="kokoro-menu-anchor">
          <button className="line-icon-button" onClick={() => setMenuOpen(value => !value)} aria-label="更多入口" aria-expanded={menuOpen}><MoreHorizontal size={19} /></button>
          {menuOpen && <div className="kokoro-more-menu">
            <button onClick={() => navigate('memory')}><Waves size={15} />工程记忆</button>
            <button onClick={() => navigate('audit')}><History size={15} />任务与验证</button>
            <button onClick={() => navigate('files')}><Files size={15} />项目文件</button>
            <button onClick={() => navigate('extensions')}><Plug size={15} />工具与插件</button>
            <button onClick={() => navigate('settings')}><Info size={15} />设置与关于</button>
          </div>}
        </div>
        <button className="line-icon-button kokoro-close" onClick={props.onClose} aria-label="关闭摘要栏"><X size={18} /></button>
      </>} />
    <ConversationSummary hasConversation={props.hasConversation} context={props.context} busy={props.busy} pendingCount={props.pendingCount} recoverable={props.recoverable} verification={props.verification} onCompact={props.onCompact} />
  </aside>
}
