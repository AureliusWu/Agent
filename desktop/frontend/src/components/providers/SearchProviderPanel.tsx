import { useCallback, useEffect, useRef, useState } from 'react'
import { Globe2, ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '../../api'
import { deleteDesktopSecret, hasDesktopSecret, saveDesktopSecret } from '../../secrets'

type ProviderState = { name: 'tavily' | 'brave'; configured: boolean; default: boolean }

export function SearchProviderPanel() {
  const [providers, setProviders] = useState<ProviderState[]>([])
  const [keys, setKeys] = useState({ tavily: '', brave: '' })
  const [saved, setSaved] = useState({ tavily: false, brave: false })
  const [status, setStatus] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<'tavily' | 'brave' | null>(null)
  const mounted = useRef(true)
  const revision = useRef(0)
  const running = useRef(false)

  const load = useCallback(async () => {
    const request = ++revision.current
    const data = await api<{ providers: ProviderState[] }>('/api/search/providers')
    if (!mounted.current || request !== revision.current) return
    const [tavily, brave] = await Promise.all([hasDesktopSecret('tavily_api_key'), hasDesktopSecret('brave_api_key')])
    if (!mounted.current || request !== revision.current) return
    setProviders(data.providers)
    setSaved({ tavily, brave })
    setStatus(current => ({ ...current, load: '' }))
  }, [])

  useEffect(() => {
    mounted.current = true
    const pending = load()
    const request = revision.current
    void pending.catch(() => { if (mounted.current && request === revision.current) setStatus(current => ({ ...current, load: '搜索配置读取失败，请稍后重试。' })) })
    return () => { mounted.current = false; revision.current += 1 }
  }, [load])

  async function save(name: 'tavily' | 'brave') {
    const value = keys[name].trim()
    if (!value || running.current || !mounted.current) return
    running.current = true; revision.current += 1; setBusy(name)
    try {
      try { await saveDesktopSecret(`${name}_api_key`, value) }
      catch { if (mounted.current) setStatus(current => ({ ...current, [name]: '密钥未能保存，请稍后重试。' })); return }
      if (!mounted.current) return
      setKeys(current => ({ ...current, [name]: '' }))
      setSaved(current => ({ ...current, [name]: true }))
      let message: string
      try {
        const health = await api<{ status: string; latency_ms?: number }>(`/api/search/providers/${name}/health`, { method: 'POST' })
        message = health.status === 'ok' ? `密钥已保存 · 连接正常 · ${health.latency_ms ?? '-'} ms` : '密钥已保存，但连接检查未通过。请核对配置或稍后重试。'
      } catch { message = '密钥已保存，但连接检查失败。请稍后重试。' }
      if (!mounted.current) return
      try { await load() } catch { message += ' 配置状态刷新失败。' }
      if (mounted.current) setStatus(current => ({ ...current, [name]: message }))
    } finally { running.current = false; if (mounted.current) setBusy(null) }
  }

  async function remove(name: 'tavily' | 'brave') {
    if (running.current || !mounted.current) return
    running.current = true; revision.current += 1; setBusy(name)
    try {
      try { await deleteDesktopSecret(`${name}_api_key`) }
      catch { if (mounted.current) setStatus(current => ({ ...current, [name]: '密钥删除失败，请稍后重试。' })); return }
      if (!mounted.current) return
      setSaved(current => ({ ...current, [name]: false }))
      setProviders(current => current.map(provider => provider.name === name ? { ...provider, configured: false } : provider))
      let message = '密钥已删除'
      try { await load() } catch { message += '，配置状态刷新失败。' }
      if (mounted.current) setStatus(current => ({ ...current, [name]: message }))
    } finally { running.current = false; if (mounted.current) setBusy(null) }
  }

  return <section className="provider-card search-provider-card" aria-busy={Boolean(busy)}>
    <header className="provider-heading"><span><Globe2 size={18} /></span><div><h3>联网搜索</h3><p>Tavily 默认，Brave 用于独立切换与 A/B 诊断</p></div></header>
    {(['tavily', 'brave'] as const).map(name => {
      const provider = providers.find(item => item.name === name)
      return <div className="search-provider-row" key={name}>
        <div><strong>{name === 'tavily' ? 'Tavily' : 'Brave Search'}</strong><small>{provider?.default ? '默认供应商' : '备用供应商'} · {provider?.configured || saved[name] ? '已配置' : '未配置'}</small></div>
        <input type="password" aria-label={`${name} API Key`} autoComplete="off" value={keys[name]} disabled={busy === name} onChange={event => setKeys(current => ({ ...current, [name]: event.target.value }))} placeholder={saved[name] ? '输入可替换密钥' : 'API Key'} />
        <button className="secondary" disabled={Boolean(busy) || !keys[name].trim()} onClick={() => void save(name)}><ShieldCheck size={14} />{busy === name ? '处理中…' : '保存并检查'}</button>
        {saved[name] && <button className="secondary" disabled={Boolean(busy)} onClick={() => void remove(name)} aria-label={`删除 ${name} 密钥`}><Trash2 size={14} /></button>}
        {status[name] && <p role="status">{status[name]}</p>}
      </div>
    })}
    {status.load && <p className="provider-note">{status.load}</p>}
  </section>
}
