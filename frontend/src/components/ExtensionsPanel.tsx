import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { CheckCircle2, CircleAlert, FilePlus2, PackageCheck, Plug, Plus, Power, RefreshCw, RotateCcw, Sparkles, Trash2 } from 'lucide-react'
import { api } from '../api'
import type { ExtensionPackage } from '../types'
import { DeepSeekProviderPanel } from './DeepSeekProviderPanel'
import { MemoryManager } from './MemoryManager'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

interface Skill {
  name: string
  description: string
  path: string
  enabled: boolean
  source?: 'workspace' | 'extension'
  extension_id?: string | null
}

interface Mcp {
  id: number
  name: string
  transport: string
  url: string
  enabled: boolean
  health_status: 'untested' | 'healthy' | 'error'
  tool_count: number
  tool_names: string[]
  last_error?: string | null
  last_checked_at?: string | null
}

export function ExtensionsPanel({ workspace, onChanged }: { workspace: string; onChanged: () => void }) {
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcps, setMcps] = useState<Mcp[]>([])
  const [packages, setPackages] = useState<ExtensionPackage[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [sourcePath, setSourcePath] = useState('')
  const [packageError, setPackageError] = useState('')
  const [mcpError, setMcpError] = useState('')
  const [testingMcp, setTestingMcp] = useState<number | null>(null)

  const load = () => {
    api<Skill[]>(`/api/skills?workspace=${encodeURIComponent(workspace)}`).then(setSkills).catch(() => {})
    api<Mcp[]>('/api/mcp').then(setMcps).catch(() => {})
    api<ExtensionPackage[]>('/api/extensions/packages').then(setPackages).catch(() => {})
  }

  useEffect(load, [workspace])

  async function addMcp(event: FormEvent) {
    event.preventDefault()
    setMcpError('')
    try {
      const created = await api<Mcp>('/api/mcp', { method: 'POST', body: JSON.stringify({ name, transport: 'http', url, args: [] }) })
      setName('')
      setUrl('')
      if (created.health_status !== 'healthy') setMcpError(created.last_error || '服务已保存但未通过工具发现，因此保持停用')
      load()
    } catch (caught) {
      setMcpError((caught as Error).message)
    }
  }

  async function installPackage(event: FormEvent) {
    event.preventDefault()
    setPackageError('')
    try {
      await api('/api/extensions/packages', {
        method: 'POST',
        body: JSON.stringify({ workspace, source_path: sourcePath, enable: true }),
      })
      setSourcePath('')
      load()
      onChanged()
    } catch (caught) {
      setPackageError((caught as Error).message)
    }
  }

  async function togglePackage(item: ExtensionPackage) {
    setPackageError('')
    try {
      await api(`/api/extensions/packages/${encodeURIComponent(item.extension_id)}/${encodeURIComponent(item.version)}/enabled`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: !item.enabled }),
      })
      load()
      onChanged()
    } catch (caught) {
      setPackageError((caught as Error).message)
    }
  }

  async function rollbackPackage(item: ExtensionPackage) {
    setPackageError('')
    try {
      await api(`/api/extensions/packages/${encodeURIComponent(item.extension_id)}/rollback`, { method: 'POST' })
      load()
      onChanged()
    } catch (caught) {
      setPackageError((caught as Error).message)
    }
  }

  async function toggleSkill(item: Skill) {
    await api(`/api/skills/enabled?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(item.path)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled: !item.enabled }),
    })
    load()
  }

  async function toggleMcp(item: Mcp) {
    setMcpError('')
    try {
      await api(`/api/mcp/${item.id}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled: !item.enabled }) })
      load()
    } catch (caught) {
      setMcpError((caught as Error).message)
      load()
    }
  }

  async function testMcp(item: Mcp) {
    setTestingMcp(item.id)
    setMcpError('')
    try {
      const result = await api<{ status: string; error?: string | null }>(`/api/mcp/${item.id}/test`, { method: 'POST' })
      if (result.status !== 'ok') setMcpError(result.error || '服务未返回可用工具')
      load()
    } catch (caught) {
      setMcpError((caught as Error).message)
    } finally {
      setTestingMcp(null)
    }
  }

  async function deleteMcp(id: number) {
    if (!confirm('删除这个 MCP 服务？')) return
    await api(`/api/mcp/${id}`, { method: 'DELETE' })
    load()
  }

  return <section className="content-panel">
    <PanelHeader icon={<Plug />} title="扩展能力" subtitle="模型、专业 Agent、扩展包、Skill、MCP 与记忆" />
    <div className="extension-grid">
      <div>
        <DeepSeekProviderPanel />

        <h3>已挂载 Skill <span>{skills.length}</span></h3>
        {skills.length ? skills.map(item => <div className={`extension-row ${item.enabled ? '' : 'disabled'}`} key={item.path}>
          <span><Sparkles /></span>
          <div><strong>{item.name}</strong><p>{item.description || item.path}{item.source === 'extension' ? ` · ${item.extension_id}` : ''}</p></div>
          <button title={item.enabled ? '停用' : '启用'} onClick={() => toggleSkill(item)}><Power size={15} /></button>
        </div>) : <div className="empty-panel"><FilePlus2 /><p>在工作区 `.agent/skills/*/SKILL.md` 添加 Skill</p></div>}
      </div>

      <div>
        <h3>声明式扩展包 <span>{packages.length}</span></h3>
        {packages.map(item => <div className={`extension-row package-row ${item.enabled ? '' : 'disabled'}`} key={`${item.extension_id}@${item.version}`}>
          <span><PackageCheck /></span>
          <div>
            <strong>{item.name} <small>{item.version}</small></strong>
            <p>{item.signature_status === 'verified' ? '摘要已验证' : '未签名'} · {item.contributions.agents} Agent · {item.contributions.tools} 工具 · {item.contributions.skills} Skill</p>
            {item.last_error && <p className="extension-error">{item.last_error}</p>}
          </div>
          <button title={item.enabled ? '停用' : '启用'} onClick={() => togglePackage(item)}><Power size={15} /></button>
          {item.rollback_available && <button title="回滚到上一版" onClick={() => rollbackPackage(item)}><RotateCcw size={15} /></button>}
        </div>)}
        <form className="mcp-form" onSubmit={installPackage}>
          <input placeholder="工作区内扩展包目录，如 extensions/team-coding" value={sourcePath} onChange={event => setSourcePath(event.target.value)} required />
          <button className="primary"><Plus size={16} />校验并安装</button>
          {packageError && <p className="extension-error">{packageError}</p>}
        </form>

        <h3>MCP 服务 <span>{mcps.length}</span></h3>
        {mcps.map(item => <div className={`extension-row ${item.enabled ? '' : 'disabled'}`} key={item.id}>
          <span>{item.health_status === 'healthy' ? <CheckCircle2 /> : <CircleAlert />}</span>
          <div><strong>{item.name}</strong><p>{item.transport} · {item.url}</p><p>{item.health_status === 'healthy' ? `可用 · ${item.tool_count} 个真实工具` : item.health_status === 'error' ? `不可用 · ${item.last_error || '发现失败'}` : '尚未完成工具发现'}</p></div>
          <button title="重新测试真实工具发现" disabled={testingMcp === item.id} onClick={() => testMcp(item)}><RefreshCw size={15} /></button>
          <button title={item.enabled ? '停用' : '启用'} onClick={() => toggleMcp(item)}><Power size={15} /></button>
          <button title="删除" onClick={() => deleteMcp(item.id)}><Trash2 size={15} /></button>
        </div>)}
        <form className="mcp-form" onSubmit={addMcp}>
          <input placeholder="服务名称" value={name} onChange={event => setName(event.target.value)} required />
          <input placeholder="https://mcp.example.com/mcp" value={url} onChange={event => setUrl(event.target.value)} required />
          <button className="primary"><Plus size={16} />连接 HTTP MCP</button>
          {mcpError && <p className="extension-error">{mcpError}</p>}
        </form>
      </div>
    </div>
    <MemoryManager workspace={workspace} />
  </section>
}
