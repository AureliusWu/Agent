import { Waves } from 'lucide-react'
import { MemoryManager } from './MemoryManager'
import { LongTermMemoryManager } from './LongTermMemoryManager'
import { AffectStatePanel } from '../kokoro/AffectStatePanel'
import { PanelHeader } from '../shared/PanelHeader'
import '../../styles/panels.css'

export function MemoryPanel({ workspace, initialQuery = '', focusMemoryId = '' }: { workspace: string; initialQuery?: string; focusMemoryId?: string }) {
  return <section className="content-panel">
    <PanelHeader icon={<Waves />} title="记忆" subtitle="管理你自己编写的记忆，或当前项目的可验证记忆" />
    <AffectStatePanel />
    <LongTermMemoryManager initialQuery={initialQuery} focusMemoryId={focusMemoryId} />
    <details className="legacy-memory-section"><summary>工程记忆兼容层</summary>
    <MemoryManager workspace={workspace} />
    </details>
  </section>
}
