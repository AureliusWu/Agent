import { useCallback, useEffect, useState } from 'react'
import { Globe2, ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '../../api'
import { deleteDesktopSecret, hasDesktopSecret, isDesktop, saveDesktopSecret } from '../../secrets'

type ProviderState = { name: 'tavily' | 'brave'; configured: boolean; default: boolean }

export function SearchProviderPanel() {
  const desktop = isDesktop()
  const [providers, setProviders] = useState<ProviderState[]>([])
  const [keys, setKeys] = useState({ tavily: '', brave: '' })
  const [saved, setSaved] = useState({ tavily: false, brave: false })
  const [status, setStatus] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    const data = await api<{ providers: ProviderState[] }>('/api/search/providers')
    setProviders(data.providers)
    if (desktop) {
      const [tavily, brave] = await Promise.all([hasDesktopSecret('tavily_api_key'), hasDesktopSecret('brave_api_key')])
      setSaved({ tavily, brave })
    }
  }, [desktop])

  useEffect(() => { void load().catch(error => setStatus({ load: error.message })) }, [load])

  async function save(name: 'tavily' | 'brave') {
    const value = keys[name].trim()
    if (!value) return
    await saveDesktopSecret(`${name}_api_key`, value)
    setKeys(current => ({ ...current, [name]: '' }))
    setSaved(current => ({ ...current, [name]: true }))
    const health = await api<{ status: string; latency_ms?: number; error?: string }>(`/api/search/providers/${name}/health`)
    setStatus(current => ({ ...current, [name]: health.status === 'ok' ? `连接正常 · ${health.latency_ms ?? '-'} ms` : `连接失败 · ${health.error || health.status}` }))
    await load()
  }

  async function remove(name: 'tavily' | 'brave') {
    await deleteDesktopSecret(`${name}_api_key`)
    setSaved(current => ({ ...current, [name]: false }))
    setStatus(current => ({ ...current, [name]: '密钥已删除' }))
    await load()
  }

  return <section className="provider-card search-provider-card">
    <header className="provider-heading"><span><Globe2 size={18} /></span><div><h3>联网搜索</h3><p>Tavily 默认，Brave 用于独立切换与 A/B 诊断</p></div></header>
    {(['tavily', 'brave'] as const).map(name => {
      const provider = providers.find(item => item.name === name)
      return <div className="search-provider-row" key={name}>
        <div><strong>{name === 'tavily' ? 'Tavily' : 'Brave Search'}</strong><small>{provider?.default ? '默认供应商' : '备用供应商'} · {provider?.configured || saved[name] ? '已配置' : '未配置'}</small></div>
        <input type="password" autoComplete="off" disabled={!desktop} value={keys[name]} onChange={event => setKeys(current => ({ ...current, [name]: event.target.value }))} placeholder={saved[name] ? '输入可替换密钥' : 'API Key'} />
        <button className="secondary" disabled={!desktop || !keys[name].trim()} onClick={() => void save(name)}><ShieldCheck size={14} />保存并检查</button>
        {saved[name] && <button className="secondary" onClick={() => void remove(name)} aria-label={`删除 ${name} 密钥`}><Trash2 size={14} /></button>}
        {status[name] && <p>{status[name]}</p>}
      </div>
    })}
    {status.load && <p className="provider-note">{status.load}</p>}
  </section>
}
