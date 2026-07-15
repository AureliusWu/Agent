import { Waves } from 'lucide-react'
import { MemoryManager } from './MemoryManager'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

export function MemoryPanel({ workspace }: { workspace: string }) {
  return <section className="content-panel">
    <PanelHeader icon={<Waves />} title="记忆海" subtitle="管理当前工作区可验证、可编辑和可遗忘的工程记忆" />
    <MemoryManager workspace={workspace} />
  </section>
}
