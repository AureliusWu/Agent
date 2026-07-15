import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { FilePlus2, Gauge, PackageCheck, Plug, Plus, Power, RotateCcw, Shield, Sparkles, Trash2 } from 'lucide-react'
import { api } from '../api'
import { isDesktop, saveDesktopSecret } from '../secrets'
import type { ExtensionPackage, ProviderHealth, ProviderPolicy } from '../types'
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
  enabled: number
}

export function ExtensionsPanel({ workspace, onChanged }: { workspace: string; onChanged: () => void }) {
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcps, setMcps] = useState<Mcp[]>([])
  const [packages, setPackages] = useState<ExtensionPackage[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [sourcePath, setSourcePath] = useState('')
  const [packageError, setPackageError] = useState('')
  const [modelKey, setModelKey] = useState('')
  const [keyStatus, setKeyStatus] = useState('')
  const [health, setHealth] = useState<ProviderHealth | null>(null)
  const [providerPolicy, setProviderPolicy] = useState<ProviderPolicy | null>(null)

  const load = () => {
    api<Skill[]>(`/api/skills?workspace=${encodeURIComponent(workspace)}`).then(setSkills).catch(() => {})
    api<Mcp[]>('/api/mcp').then(setMcps).catch(() => {})
    api<ExtensionPackage[]>('/api/extensions/packages').then(setPackages).catch(() => {})
    api<ProviderPolicy>('/api/provider/policy').then(setProviderPolicy).catch(() => {})
  }

  useEffect(load, [workspace])

  async function addMcp(event: FormEvent) {
    event.preventDefault()
    await api('/api/mcp', { method: 'POST', body: JSON.stringify({ name, transport: 'http', url, args: [] }) })
    setName('')
    setUrl('')
    load()
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

  async function checkHealth() {
    try {
      const nextHealth = await api<ProviderHealth>('/api/provider/health')
      setHealth(nextHealth)
      setProviderPolicy(await api<ProviderPolicy>('/api/provider/policy'))
    } catch (caught) {
      setHealth({ status: 'error', latency_ms: null, model: '', error: (caught as Error).message })
    }
  }

  async function saveKey(event: FormEvent) {
    event.preventDefault()
    try {
      await saveDesktopSecret('model_api_key', modelKey)
      setModelKey('')
      setKeyStatus('已保存到 Windows 凭据管理器')
      setTimeout(checkHealth, 150)
    } catch (caught) {
      setKeyStatus((caught as Error).message)
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
    await api(`/api/mcp/${item.id}/enabled`, { method: 'PATCH', body: JSON.stringify({ enabled: !item.enabled }) })
    load()
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
        <h3>模型凭据</h3>
        <form className="mcp-form" onSubmit={saveKey}>
          <input type="password" placeholder="DeepSeek / OpenAI-compatible API Key" value={modelKey} onChange={event => setModelKey(event.target.value)} required />
          <button className="primary" disabled={!isDesktop()}><Shield size={16} />{isDesktop() ? '保存到 Windows 凭据' : '网页端由后端环境变量管理'}</button>
          {keyStatus && <p>{keyStatus}</p>}
        </form>
        <button className="secondary health-button" onClick={checkHealth}><Gauge size={16} />检查模型连接</button>
        {health && <p className={`provider-health ${health.status}`}>{health.status === 'ok' ? `${health.model} 可用 · ${health.latency_ms} ms` : health.status === 'unconfigured' ? '尚未配置 API Key' : `连接失败：${health.error}`}</p>}
        {providerPolicy && <div className="provider-matrix">{providerPolicy.capability_matrix.map(item => {
          const performance = providerPolicy.model_performance[item.model]
          const labels = [['streaming', '流式'], ['native_tool_calls', '工具'], ['vision', '视觉'], ['audio', '音频'], ['reasoning_effort', '推理参数']] as const
          return <div key={`${item.provider}:${item.model}`}><header><strong>{item.model}</strong><span>{performance?.samples ? `${Math.round(performance.success_rate * 100)}% · ${performance.average_latency_ms} ms · $${performance.average_cost_usd.toFixed(4)}` : '暂无调用样本'}</span></header><p>{labels.map(([key, label]) => <span className={item.capabilities[key]} key={key}>{label} {item.capabilities[key] === 'supported' ? '可用' : item.capabilities[key] === 'unsupported' ? '不可用' : '未知'}</span>)}</p></div>
        })}</div>}

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
          <span><Plug /></span>
          <div><strong>{item.name}</strong><p>{item.transport} · {item.url}</p></div>
          <button title={item.enabled ? '停用' : '启用'} onClick={() => toggleMcp(item)}><Power size={15} /></button>
          <button title="删除" onClick={() => deleteMcp(item.id)}><Trash2 size={15} /></button>
        </div>)}
        <form className="mcp-form" onSubmit={addMcp}>
          <input placeholder="服务名称" value={name} onChange={event => setName(event.target.value)} required />
          <input placeholder="https://mcp.example.com/mcp" value={url} onChange={event => setUrl(event.target.value)} required />
          <button className="primary"><Plus size={16} />连接 HTTP MCP</button>
        </form>
      </div>
    </div>
    <MemoryManager workspace={workspace} />
  </section>
}
