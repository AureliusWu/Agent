import { Activity, BrainCircuit, Database, Folder, ShieldCheck, Sparkles } from 'lucide-react'
import { MODE_LABEL } from '../constants'
import type { BuildInfo } from '../buildInfo'
import type { ContextStats, Conversation, PermissionMode } from '../types'
import '../styles/layout.css'

interface Props {
  active: Conversation | null
  mode: PermissionMode
  context: ContextStats | null
  apiOnline: boolean
  busy: boolean
  profileName: string
  buildInfo: BuildInfo
}

export function ContextPanel(props: Props) {
  return <aside className="context-panel">
    <header>
      <span>CONTEXT LAYER</span>
      <h2>当前上下文</h2>
      <p>只展示真实连接、任务和运行环境信息。</p>
    </header>

    <section className="context-section">
      <h3>Core Status</h3>
      <div className="context-row"><span><Database /></span><div><strong>司忆核心</strong><small>{props.apiOnline ? '后端已连接' : '后端未连接'}</small></div></div>
      <div className="context-row"><span><Sparkles /></span><div><strong>夏目心</strong><small>{props.busy ? '正在回应与执行' : '等待管理员指令'}</small></div></div>
      <div className="context-row"><span><Activity /></span><div><strong>运行环境</strong><small>{props.buildInfo.environment} · v{props.buildInfo.version}</small></div></div>
    </section>

    <section className="context-section">
      <h3>Conversation</h3>
      <div className="context-row"><span><BrainCircuit /></span><div><strong>{props.profileName}</strong><small>{props.active?.title || '尚未创建对话'}</small></div></div>
      <div className="context-row"><span><ShieldCheck /></span><div><strong>{MODE_LABEL[props.mode]}</strong><small>当前执行权限</small></div></div>
      <div className="context-row"><span><Folder /></span><div><strong>工作区</strong><small>{props.active?.workspace || '未选择'}</small></div></div>
    </section>

    <section className="context-section">
      <h3>Memory Window</h3>
      <div className="context-row"><span><BrainCircuit /></span><div><strong>{props.context ? `约 ${props.context.estimated_tokens.toLocaleString()} tokens` : '尚无上下文统计'}</strong><small>{props.context ? `${props.context.message_count} 条消息${props.context.has_summary ? ' · 已压缩' : ''}` : '创建对话后开始统计'}</small></div></div>
    </section>

    <p className="context-footnote">记忆海只在任务需要时加载相关工程记忆；这里不会显示未经后端确认的伪状态或虚构进度。</p>
  </aside>
}
