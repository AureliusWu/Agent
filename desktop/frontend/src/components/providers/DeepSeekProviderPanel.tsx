import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ExternalLink, Gauge, KeyRound, ShieldCheck, Trash2 } from 'lucide-react'
import { api } from '../../api'
import { deleteDesktopSecret, hasDesktopSecret, saveDesktopSecret } from '../../secrets'
import type { ProviderConfiguration, ProviderHealth, ProviderPolicy } from '../../types'
import '../../styles/provider.css'

const CAPABILITY_LABELS = [
  ['streaming', '流式输出'],
  ['native_tool_calls', '工具调用'],
  ['vision', '视觉'],
  ['audio', '音频'],
  ['reasoning_effort', '推理强度'],
] as const

export function DeepSeekProviderPanel() {
  const [policy, setPolicy] = useState<ProviderPolicy | null>(null)
  const [health, setHealth] = useState<ProviderHealth | null>(null)
  const [modelKey, setModelKey] = useState('')
  const [keySaved, setKeySaved] = useState(false)
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(false)
  const [configuration, setConfiguration] = useState<ProviderConfiguration | null>(null)

  useEffect(() => {
    api<ProviderPolicy>('/api/provider/policy').then(setPolicy).catch(error => setStatus(error.message))
    api<ProviderConfiguration>('/api/provider/configuration').then(setConfiguration).catch(error => setStatus(error.message))
    hasDesktopSecret('model_api_key').then(setKeySaved).catch(() => setKeySaved(false))
  }, [])

  async function saveConfiguration(event: FormEvent) {
    event.preventDefault()
    if (!configuration) return
    setLoading(true)
    setStatus('')
    try {
      const saved = await api<ProviderConfiguration>('/api/provider/configuration', {
        method: 'PUT',
        body: JSON.stringify(configuration),
      })
      setConfiguration(saved)
      setPolicy(await api<ProviderPolicy>('/api/provider/policy'))
      setStatus(`Provider 已切换为 ${saved.provider_id}，新任务将使用该配置。`)
      setHealth(null)
    } catch (caught) {
      setStatus(`Provider 配置未保存：${(caught as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  async function checkHealth() {
    setLoading(true)
    setStatus('')
    try {
      const result = await api<ProviderHealth>('/api/provider/health')
      setHealth(result)
      setPolicy(await api<ProviderPolicy>('/api/provider/policy'))
    } catch (caught) {
      setHealth({ status: 'error', latency_ms: null, model: '', error: (caught as Error).message })
    } finally {
      setLoading(false)
    }
  }

  async function saveKey(event: FormEvent) {
    event.preventDefault()
    const value = modelKey.trim()
    if (!value) return
    setLoading(true)
    setStatus('')
    try {
      const result = await api<ProviderHealth>('/api/provider/health', {
        headers: { 'X-Model-Api-Key': value },
      })
      setHealth(result)
      if (result.status !== 'ok') {
        throw new Error(result.error || 'DeepSeek 拒绝了这个 API Key')
      }
      await saveDesktopSecret('model_api_key', value)
      setModelKey('')
      setKeySaved(true)
      setStatus(`API Key 已验证并保存到 Windows 凭据管理器，${result.model} 可用。`)
    } catch (caught) {
      setStatus(`API Key 未保存：${(caught as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  async function clearKey() {
    setLoading(true)
    setStatus('')
    try {
      await deleteDesktopSecret('model_api_key')
      setKeySaved(false)
      setModelKey('')
      setHealth({ status: 'unconfigured', latency_ms: null, model: '' })
      setStatus('已从 Windows 凭据管理器删除 API Key。')
    } catch (caught) {
      setStatus(`删除 API Key 失败：${(caught as Error).message}`)
    } finally {
      setLoading(false)
    }
  }

  const profile = policy?.provider
  return <section className="provider-card" aria-labelledby="provider-title">
    <header className="provider-heading">
      <span><KeyRound size={18} /></span>
      <div>
        <h3 id="provider-title">{profile?.name || 'DeepSeek'}</h3>
        <p>当前主要基座模型供应商</p>
      </div>
      {profile?.official_url && <a href={profile.official_url} target="_blank" rel="noreferrer">官网<ExternalLink size={13} /></a>}
    </header>

    <dl className="provider-facts">
      <div><dt>API 格式</dt><dd>{profile?.api_format || 'OpenAI-compatible'}</dd></div>
      <div><dt>请求地址</dt><dd><code>{profile?.request_url || '加载中...'}</code></dd></div>
      <div><dt>对话端点</dt><dd><code>{profile?.chat_endpoint || '加载中...'}</code></dd></div>
      <div><dt>默认模型</dt><dd><code>{profile?.default_model || '加载中...'}</code></dd></div>
    </dl>

    {configuration && <form className="provider-key-form" onSubmit={saveConfiguration}>
      <label htmlFor="model-provider">模型 Provider</label>
      <select
        id="model-provider"
        value={configuration.provider_id}
        onChange={event => {
          const providerId = event.target.value as ProviderConfiguration['provider_id']
          setConfiguration({
            ...configuration,
            provider_id: providerId,
            base_url: providerId === 'ollama' ? 'http://127.0.0.1:11434' : '',
            model: providerId === 'ollama' ? 'qwen3:4b' : '',
          })
        }}
      >
        <option value="deepseek">DeepSeek（云端，可能产生费用）</option>
        <option value="ollama">Ollama（本机 qwen3:4b）</option>
        <option value="mock">Mock（确定性测试）</option>
      </select>
      <label htmlFor="provider-timeout">超时（秒）</label>
      <input
        id="provider-timeout"
        type="number"
        min={1}
        max={600}
        value={configuration.timeout_seconds}
        onChange={event => setConfiguration({ ...configuration, timeout_seconds: Number(event.target.value) })}
      />
      <label htmlFor="provider-max-tokens">最大输出 Token</label>
      <input
        id="provider-max-tokens"
        type="number"
        min={1}
        max={1000000}
        value={configuration.max_tokens}
        onChange={event => setConfiguration({ ...configuration, max_tokens: Number(event.target.value) })}
      />
      <label><input type="checkbox" checked={configuration.allow_streaming} onChange={event => setConfiguration({ ...configuration, allow_streaming: event.target.checked })} />流式输出</label>
      <label><input type="checkbox" checked={configuration.allow_tools} onChange={event => setConfiguration({ ...configuration, allow_tools: event.target.checked })} />工具调用</label>
      <button className="secondary" type="submit" disabled={loading}>保存 Provider 配置</button>
    </form>}

    <div className="provider-models">
      <strong>可用模型</strong>
      <div>{profile?.models.map(model => <code key={model}>{model}</code>)}</div>
      {profile?.docs_url && <a href={profile.docs_url} target="_blank" rel="noreferrer">DeepSeek API 文档<ExternalLink size={12} /></a>}
    </div>

    {(!configuration || configuration.provider_id === 'deepseek') && <form className="provider-key-form" onSubmit={saveKey}>
      <label htmlFor="deepseek-api-key">API Key</label>
      <input
        id="deepseek-api-key"
        type="password"
        autoComplete="off"
        placeholder={keySaved ? '已保存在 Windows 凭据管理器，输入可替换' : '输入 DeepSeek API Key'}
        value={modelKey}
        onChange={event => setModelKey(event.target.value)}
      />
      <button className="secondary" type="submit" disabled={!modelKey.trim() || loading}>
        <ShieldCheck size={15} />{keySaved ? '替换密钥' : '保存密钥'}
      </button>
      {keySaved && <button className="secondary provider-delete-key" type="button" onClick={clearKey} disabled={loading}>
        <Trash2 size={14} />删除密钥
      </button>}
    </form>}
    {status && <p className="provider-note">{status}</p>}

    <button className="secondary provider-health-button" type="button" onClick={checkHealth} disabled={loading}>
      <Gauge size={15} />{loading ? '正在检查...' : '检查连接'}
    </button>
    {health && <p className={`provider-health ${health.status}`}>
      {health.status === 'ok'
        ? `${health.model} 可用 · ${health.latency_ms} ms`
        : health.status === 'unconfigured'
          ? '尚未配置 API Key'
          : `连接失败：${health.error || '未知错误'}`}
    </p>}

    {policy && <div className="provider-matrix">{policy.capability_matrix.map(item => {
      const performance = policy.model_performance[item.model]
      return <div key={`${item.provider}:${item.model}`}>
        <header><strong>{item.model}</strong><span>{performance?.samples ? `${Math.round(performance.success_rate * 100)}% · ${performance.average_latency_ms} ms` : '暂无调用样本'}</span></header>
        <p>{CAPABILITY_LABELS.map(([key, label]) => <span className={item.capabilities[key]} key={key}>{label} · {item.capabilities[key] === 'supported' ? '可用' : item.capabilities[key] === 'unsupported' ? '不可用' : '待验证'}</span>)}</p>
      </div>
    })}</div>}
  </section>
}
