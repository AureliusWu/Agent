import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { CheckCircle2, CircleAlert, FilePlus2, PackageCheck, Plug, Plus, Power, RefreshCw, RotateCcw, ShieldCheck, Sparkles, Trash2 } from 'lucide-react'
import { api } from '../../api'
import type { ExtensionPackage } from '../../types'
import { DeepSeekProviderPanel } from '../providers/DeepSeekProviderPanel'
import { SearchProviderPanel } from '../providers/SearchProviderPanel'
import { LocalAiPanel } from '../providers/LocalAiPanel'
import { MemoryManager } from '../memory/MemoryManager'
import { PanelHeader } from '../shared/PanelHeader'
import { managementActionHeaders } from '../../adminActionGrants'
import type { AdminManagementOperation } from '../../adminActionGrants'
import { mcpSecretBindingPayload } from '../../shared/mcpSecretBinding'
import { PluginLibraryPanel } from './PluginLibraryPanel'
import '../../styles/panels.css'

interface Skill {
  name: string
  version: string
  description: string
  path: string
  enabled: boolean
  source: 'builtin' | 'workspace' | 'extension'
  extension_id?: string | null
  status: 'ready' | 'invalid' | 'conflict'
  error?: string | null
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
  secret_binding?: string | null
}

interface SecurityPolicy {
  domain: 'normal' | 'developer' | 'administrator'
  runtime_package_install: boolean
  command_allowlist: string[]
  grants: Array<{
    id: number
    permission: string
    effect: 'allow' | 'deny'
    scope: 'workspace' | 'always'
    workspace: string
    tool: string
    source: string
  }>
}

export function ExtensionsPanel({ workspace, conversationId, onChanged }: { workspace: string; conversationId: number | null; onChanged: () => void }) {
  const [skills, setSkills] = useState<Skill[]>([])
  const [mcps, setMcps] = useState<Mcp[]>([])
  const [packages, setPackages] = useState<ExtensionPackage[]>([])
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [secretBinding, setSecretBinding] = useState('')
  const [sourcePath, setSourcePath] = useState('')
  const [packageError, setPackageError] = useState('')
  const [skillError, setSkillError] = useState('')
  const [mcpError, setMcpError] = useState('')
  const [testingMcp, setTestingMcp] = useState<number | null>(null)
  const [security, setSecurity] = useState<SecurityPolicy | null>(null)
  const [securityError, setSecurityError] = useState('')

  const load = () => {
    api<Skill[]>(`/api/skills?workspace=${encodeURIComponent(workspace)}`).then(setSkills).catch(() => {})
    api<Mcp[]>('/api/mcp').then(setMcps).catch(() => {})
    api<ExtensionPackage[]>('/api/extensions/packages').then(setPackages).catch(() => {})
    api<SecurityPolicy>('/api/security/policy').then(setSecurity).catch(() => {})
  }

  useEffect(load, [workspace])

  async function approveAdminAction(
    operation: AdminManagementOperation,
    targetId: string,
    payload: Record<string, unknown>,
    promptText: string,
  ): Promise<Record<string, string> | null> {
    if (!conversationId) throw new Error('请先选择一个对话，再执行管理员操作')
    if (!confirm(promptText)) return null
    return managementActionHeaders(operation, targetId, payload, conversationId)
  }

  async function addMcp(event: FormEvent) {
    event.preventDefault()
    setMcpError('')
    try {
      const payload = { name, transport: 'http', url, command: null, args: [], ...mcpSecretBindingPayload(secretBinding) }
      const headers = await approveAdminAction('mcp.register', 'new', payload, `注册并测试 MCP 服务“${name}”？`)
      if (!headers) return
      const created = await api<Mcp>('/api/mcp', { method: 'POST', headers, body: JSON.stringify(payload) })
      setName('')
      setUrl('')
      setSecretBinding('')
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
      const payload = { workspace, source_path: sourcePath, enable: true }
      const headers = await approveAdminAction('extension.install', 'new', payload, `安装并启用扩展包“${sourcePath}”？`)
      if (!headers) return
      await api('/api/extensions/packages', {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
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
      const enabled = !item.enabled
      const payload = { enabled }
      const operation = enabled ? 'extension.enable' : 'extension.disable'
      const headers = await approveAdminAction(operation, `${item.extension_id}@${item.version}`, payload, `${enabled ? '启用' : '停用'}扩展“${item.name} ${item.version}”？`)
      if (!headers) return
      await api(`/api/extensions/packages/${encodeURIComponent(item.extension_id)}/${encodeURIComponent(item.version)}/enabled`, {
        method: 'PATCH',
        headers,
        body: JSON.stringify(payload),
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
      const headers = await approveAdminAction('extension.rollback', item.extension_id, {}, `将扩展“${item.name}”回滚到上一版本？`)
      if (!headers) return
      await api(`/api/extensions/packages/${encodeURIComponent(item.extension_id)}/rollback`, { method: 'POST', headers })
      load()
      onChanged()
    } catch (caught) {
      setPackageError((caught as Error).message)
    }
  }

  async function uninstallPackage(item: ExtensionPackage) {
    setPackageError('')
    try {
      const targetId = `${item.extension_id}@${item.version}`
      const headers = await approveAdminAction('extension.uninstall', targetId, {}, `卸载扩展“${item.name} ${item.version}”？安装文件将移入可恢复归档。`)
      if (!headers) return
      await api(`/api/extensions/packages/${encodeURIComponent(item.extension_id)}/${encodeURIComponent(item.version)}`, {
        method: 'DELETE',
        headers,
      })
      load()
      onChanged()
    } catch (caught) {
      setPackageError((caught as Error).message)
    }
  }

  async function toggleSkill(item: Skill) {
    setSkillError('')
    try {
      const enabled = !item.enabled
      const payload = { workspace, path: item.path, enabled }
      const operation = enabled ? 'skill.enable' : 'skill.disable'
      const headers = await approveAdminAction(operation, item.path, payload, `${enabled ? '启用' : '停用'} Skill“${item.name}”？`)
      if (!headers) return
      await api(`/api/skills/enabled?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(item.path)}`, {
        method: 'PATCH',
        headers,
        body: JSON.stringify({ enabled }),
      })
      load()
    } catch (caught) {
      setSkillError((caught as Error).message)
    }
  }

  async function uninstallSkill(item: Skill) {
    setSkillError('')
    try {
      const payload = { workspace, path: item.path }
      const headers = await approveAdminAction('skill.uninstall', item.path, payload, `卸载 ${item.name} v${item.version}？原文件将移入可恢复归档。`)
      if (!headers) return
      await api(`/api/skills?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(item.path)}`, {
        method: 'DELETE',
        headers,
      })
      load()
      onChanged()
    } catch (caught) {
      setSkillError((caught as Error).message)
    }
  }

  async function changeSecurityDomain(domain: SecurityPolicy['domain']) {
    if (!confirm(`切换到 ${domain} 安全域？此操作会改变可见的开发/管理能力。`)) return
    setSecurityError('')
    try {
      const result = await api<SecurityPolicy>('/api/security/domain', {
        method: 'PUT',
        body: JSON.stringify({ domain, administrator_confirmed: true }),
      })
      setSecurity(result)
      load()
    } catch (caught) {
      setSecurityError((caught as Error).message)
    }
  }

  async function revokePermission(id: number) {
    if (!confirm('撤销这条持久权限？后续操作将重新请求确认。')) return
    setSecurityError('')
    try {
      await api(`/api/security/permissions/${id}?administrator_confirmed=true`, { method: 'DELETE' })
      load()
    } catch (caught) {
      setSecurityError((caught as Error).message)
    }
  }

  async function toggleMcp(item: Mcp) {
    setMcpError('')
    try {
      const enabled = !item.enabled
      const payload = { enabled }
      const operation = enabled ? 'mcp.enable' : 'mcp.disable'
      const headers = await approveAdminAction(operation, String(item.id), payload, `${enabled ? '启用' : '停用'} MCP 服务“${item.name}”？`)
      if (!headers) return
      await api(`/api/mcp/${item.id}/enabled`, { method: 'PATCH', headers, body: JSON.stringify(payload) })
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
      const headers = await approveAdminAction('mcp.test', String(item.id), {}, `测试 MCP 服务“${item.name}”？这会连接并执行工具发现。`)
      if (!headers) return
      const result = await api<{ status: string; error?: string | null }>(`/api/mcp/${item.id}/test`, { method: 'POST', headers })
      if (result.status !== 'ok') setMcpError(result.error || '服务未返回可用工具')
      load()
    } catch (caught) {
      setMcpError((caught as Error).message)
    } finally {
      setTestingMcp(null)
    }
  }

  async function deleteMcp(id: number) {
    setMcpError('')
    try {
      const headers = await approveAdminAction('mcp.delete', String(id), {}, '删除这个 MCP 服务？')
      if (!headers) return
      await api(`/api/mcp/${id}`, { method: 'DELETE', headers })
      load()
    } catch (caught) {
      setMcpError((caught as Error).message)
    }
  }

  return <section className="content-panel">
    <PanelHeader icon={<Plug />} title="扩展能力" subtitle="插件库、模型、扩展包、Skill、MCP 与记忆" />
    <PluginLibraryPanel workspace={workspace} />
    <div className="extension-grid">
      <div>
        <DeepSeekProviderPanel />
        <SearchProviderPanel />
        <LocalAiPanel />

        <h3>安全域与权限 <span>{security?.grants.length || 0}</span></h3>
        {security&&<div className="extension-row">
          <span><ShieldCheck /></span>
          <div>
            <strong>{security.domain}</strong>
            <p>运行时依赖安装：禁止 · 命令允许列表 {security.command_allowlist.length} 项</p>
          </div>
          {(['normal', 'developer', 'administrator'] as const).map(domain=>
            <button key={domain} disabled={security.domain===domain} title={`切换到 ${domain}`} onClick={()=>changeSecurityDomain(domain)}>{domain.slice(0,3)}</button>
          )}
        </div>}
        {security?.grants.map(grant=><div className="extension-row" key={grant.id}>
          <span><ShieldCheck /></span>
          <div>
            <strong>{grant.effect === 'deny' ? '拒绝' : '允许'} · {grant.permission}</strong>
            <p>{grant.scope} · {grant.workspace || '全部工作区'} · {grant.source}:{grant.tool}</p>
          </div>
          <button title="撤销权限" onClick={()=>revokePermission(grant.id)}><Trash2 size={15}/></button>
        </div>)}
        {securityError&&<p className="extension-error">{securityError}</p>}

        <h3>已挂载 Skill <span>{skills.length}</span></h3>
        {skills.length ? skills.map(item => <div className={`extension-row ${item.enabled && item.status === 'ready' ? '' : 'disabled'}`} key={item.path}>
          <span><Sparkles /></span>
          <div>
            <strong>{item.name} <small>v{item.version}</small></strong>
            <p>{item.description || item.path} · {item.source}{item.source === 'extension' ? ` · ${item.extension_id}` : ''}</p>
            {item.error && <p className="extension-error">{item.error}</p>}
          </div>
          <button title={item.status === 'ready' ? (item.enabled ? '停用' : '启用') : '存在错误，不能启用'} disabled={item.status !== 'ready'} onClick={() => toggleSkill(item)}><Power size={15} /></button>
          {item.source === 'workspace' && <button title="卸载并归档" onClick={() => uninstallSkill(item)}><Trash2 size={15} /></button>}
        </div>) : <div className="empty-panel"><FilePlus2 /><p>在工作区 `.agent/skills/*/SKILL.md` 添加 Skill</p></div>}
        {skillError&&<p className="extension-error">{skillError}</p>}
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
          <button title="卸载并归档" onClick={() => uninstallPackage(item)}><Trash2 size={15} /></button>
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
          <label>密钥环境变量引用（可选）<input aria-label="密钥环境变量引用" placeholder="env:MY_MCP_TOKEN（只填引用，不填密钥）" autoComplete="off" maxLength={128} value={secretBinding} onChange={event => setSecretBinding(event.target.value)} /></label>
          <button className="primary"><Plus size={16} />连接 HTTP MCP</button>
          {mcpError && <p className="extension-error">{mcpError}</p>}
        </form>
      </div>
    </div>
    <MemoryManager workspace={workspace} />
  </section>
}
