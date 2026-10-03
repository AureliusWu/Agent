import { useEffect, useState } from 'react'
import { BookOpen, Brain, Mic, PackageCheck, PenLine, RefreshCw, Terminal, Volume2 } from 'lucide-react'
import { api } from '../../api'
import '../../styles/plugins.css'

type Category = 'hear' | 'speak' | 'read' | 'write' | 'execute' | 'memory'
type Status = 'available' | 'configured' | 'unconfigured' | 'requires_workspace'
interface PluginTool { name: string; description: string; risk_level: string; status: Status }
interface CapabilityPlugin {
  id: string
  name: string
  description: string
  category: Category
  version: string
  status: Status
  reason: string
  tools: PluginTool[]
}
interface Library { schema_version: number; plugins: CapabilityPlugin[]; tool_count: number }

const categories = [
  { id: 'hear', label: '听', icon: Mic }, { id: 'speak', label: '说', icon: Volume2 },
  { id: 'read', label: '读', icon: BookOpen }, { id: 'write', label: '写', icon: PenLine },
  { id: 'execute', label: '执行', icon: Terminal }, { id: 'memory', label: '记忆', icon: Brain },
] as const
const statuses: Record<Status, string> = { available: '可用', configured: '已配置 · 待实测', unconfigured: '未配置', requires_workspace: '需要工作区' }
const risks: Record<string, string> = { low: '读取', medium: '普通写入', high: '高风险', critical: '需明确批准' }

export function PluginLibraryPanel({ workspace }: { workspace: string }) {
  const [library, setLibrary] = useState<Library | null>(null)
  const [category, setCategory] = useState<Category | 'all'>('all')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    setLibrary(null)
    api<Library>(`/api/plugins?workspace=${encodeURIComponent(workspace)}`)
      .then(result => { if (active) setLibrary(result) })
      .catch(caught => { if (active) setError((caught as Error).message) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [workspace, revision])
  const visible = library?.plugins.filter(plugin => category === 'all' || plugin.category === category) || []
  return <section className="plugin-library" aria-label="能力插件库">
    <div className="plugin-library-heading">
      <h3><PackageCheck size={18} />能力插件库 {library && <span>{library.plugins.length} 个模块 · {library.tool_count} 项工具</span>}</h3>
      <button type="button" title="刷新插件状态" aria-label="刷新插件状态" disabled={loading} onClick={() => setRevision(value => value + 1)}><RefreshCw size={15} /></button>
    </div>
    <p className="plugin-library-description">听、说、读、写与执行能力共用权限、工作区和任务记录。语音调用需明确批准，生成的声音为 AI 合成。</p>
    <div className="plugin-category-list" role="group" aria-label="筛选插件能力">
      <button type="button" aria-pressed={category === 'all'} onClick={() => setCategory('all')}>全部</button>
      {categories.map(({ id, label, icon: Icon }) => <button type="button" key={id} aria-pressed={category === id} onClick={() => setCategory(id)}><Icon size={14} />{label}</button>)}
    </div>
    {loading && <p role="status">正在读取插件库…</p>}
    {error && <p className="extension-error" role="alert">插件库读取失败：{error}。可点击刷新重试。</p>}
    <div className="plugin-card-grid">
      {visible.map(plugin => <article className="plugin-card" key={plugin.id}>
        <div className="plugin-card-heading"><strong>{plugin.name}</strong><span className={`plugin-status ${plugin.status}`}>{statuses[plugin.status]}</span></div>
        <p>{plugin.description}</p><p className="plugin-reason">{plugin.reason}</p>
        <details><summary>{plugin.tools.length} 项工具</summary>
          <ul>{plugin.tools.map(tool => <li key={tool.name}><span>{tool.description}</span><small>{tool.status === 'unconfigured' ? '未配置' : risks[tool.risk_level] || tool.risk_level}</small></li>)}</ul>
        </details>
      </article>)}
    </div>
  </section>
}
