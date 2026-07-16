import { Folder, FolderOpen, MessageSquarePlus } from 'lucide-react'
import { isDesktop } from '../secrets'
import type { Conversation } from '../types'
import { PanelHeader } from './PanelHeader'

interface Props { conversations: Conversation[]; onOpen: (workspace: string) => void }

export function ProjectsPanel({ conversations, onOpen }: Props) {
  const projects = [...new Map(conversations.filter(item => item.workspace).map(item => [item.workspace, item])).values()]
  async function chooseProject() {
    if (!isDesktop()) return
    const { open } = await import('@tauri-apps/plugin-dialog')
    const selected = await open({ directory: true, multiple: false, title: '选择司忆可以访问的项目' })
    if (typeof selected === 'string') onOpen(selected)
  }
  return <section className="content-panel projects-panel">
    <PanelHeader icon={<Folder />} title="项目" subtitle="每个项目都是一个独立、由你主动选择的工作区" />
    <div className="panel-toolbar"><button className="primary" onClick={chooseProject}><FolderOpen size={16} />打开项目</button></div>
    <div className="project-list">{projects.length ? projects.map(project => <button key={project.workspace} onClick={() => onOpen(project.workspace)}>
      <Folder size={19} /><span><strong>{project.workspace.split(/[\\/]/).filter(Boolean).pop()}</strong><small>{project.workspace}</small></span><MessageSquarePlus size={17} />
    </button>) : <div className="empty-panel"><Folder /><p>还没有项目，可以先直接聊天，需要操作文件时再打开项目。</p></div>}</div>
  </section>
}
