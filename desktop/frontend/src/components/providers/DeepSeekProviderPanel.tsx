import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ChevronDown, ExternalLink, KeyRound, Plus, RefreshCw } from 'lucide-react'
import { api } from '../../api'
import { saveDesktopSecret } from '../../secrets'
import { isLocalModelProvider, modelConnectionMessage, modelDirectoryRequest, saveModelSettings } from '../../shared/modelSettings'
import type { ProviderConfiguration, ProviderModelDirectory, ProviderPolicy } from '../../types'
import '../../styles/provider.css'

interface LocalModelChoice { model_id: string; display_name: string; installed: boolean }
interface ModelChoice { id: string; name: string }
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,199}$/

export function DeepSeekProviderPanel() {
  const [configuration, setConfiguration] = useState<ProviderConfiguration | null>(null)
  const [savedConfiguration, setSavedConfiguration] = useState<ProviderConfiguration | null>(null)
  const [policy, setPolicy] = useState<ProviderPolicy | null>(null)
  const [installedModels, setInstalledModels] = useState<LocalModelChoice[]>([])
  const [directory, setDirectory] = useState<ModelChoice[]>([])
  const [modelKey, setModelKey] = useState('')
  const [manualModel, setManualModel] = useState('')
  const [addingModel, setAddingModel] = useState(false)
  const [status, setStatus] = useState('')
  const [connection, setConnection] = useState<ProviderModelDirectory | null>(null)
  const [busy, setBusy] = useState(false)
  const lifetime = useRef(0)

  useEffect(() => {
    const generation = ++lifetime.current
    void Promise.all([
      api<ProviderConfiguration>('/api/provider/configuration'),
      api<ProviderPolicy>('/api/provider/policy'),
    ]).then(([saved, currentPolicy]) => {
      if (lifetime.current !== generation) return
      setConfiguration(saved)
      setSavedConfiguration(saved)
      setPolicy(currentPolicy)
    }).catch(() => { if (lifetime.current === generation) setStatus('无法读取模型设置，请检查本地核心状态后重新打开此页。') })
    const refreshLocalModels = () => { void api<LocalModelChoice[]>('/api/local-models/models').then(models => {
      if (lifetime.current === generation) setInstalledModels(models)
    }).catch(() => { /* Local model availability is not a cloud connection verdict. */ }) }
    refreshLocalModels()
    window.addEventListener('siyi:local-models-changed', refreshLocalModels)
    return () => { lifetime.current = generation + 1; window.removeEventListener('siyi:local-models-changed', refreshLocalModels) }
  }, [])

  const local = configuration ? isLocalModelProvider(configuration) : false
  const isOllama = configuration?.provider_id === 'ollama'
  const isMock = configuration?.provider_id === 'mock'
  const profileMatches = policy?.provider.id === configuration?.provider_id
  const known = profileMatches ? policy?.provider.models || [] : configuration?.provider_id === 'deepseek' ? ['deepseek-flash', 'deepseek-v4-pro'] : []
  const choices = new Map<string, ModelChoice>()
  for (const id of known) choices.set(id, { id, name: id })
  for (const item of directory) choices.set(item.id, item)
  if (isOllama) {
    choices.clear()
    for (const item of installedModels.filter(model => model.installed)) choices.set(item.model_id, { id: item.model_id, name: item.display_name })
  }
  if (configuration?.model && !choices.has(configuration.model)) choices.set(configuration.model, { id: configuration.model, name: configuration.model })

  function update(patch: Partial<ProviderConfiguration>) {
    if (!configuration || busy) return
    setConfiguration({ ...configuration, ...patch })
    setConnection(null)
    setStatus('')
  }

  function changeProvider(providerId: ProviderConfiguration['provider_id']) {
    if (!configuration) return
    const firstInstalledModel = installedModels.find(model => model.installed)?.model_id || ''
    update({
      provider_id: providerId,
      base_url: providerId === 'deepseek' ? 'https://api.deepseek.com' : providerId === 'ollama' ? 'http://127.0.0.1:11434' : providerId === 'openai_compatible' ? 'http://127.0.0.1:1234/v1' : '',
      model: providerId === 'deepseek' ? 'deepseek-flash' : providerId === 'ollama' ? firstInstalledModel : providerId === 'openai_compatible' ? 'local-model' : '',
      max_tokens: providerId === 'ollama' ? Math.max(configuration.max_tokens, 2048) : configuration.max_tokens,
    })
    setModelKey('')
    setDirectory([])
    setAddingModel(false)
    setManualModel('')
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!configuration || busy) return
    const generation = lifetime.current
    setBusy(true)
    setStatus('')
    try {
      const saved = await saveModelSettings(configuration, modelKey,
        value => api<ProviderConfiguration>('/api/provider/configuration', { method: 'PUT', body: JSON.stringify(value) }),
        (name, value) => saveDesktopSecret(name, value), savedConfiguration)
      if (generation !== lifetime.current) return
      setConfiguration(saved)
      setSavedConfiguration(saved)
      setModelKey('')
      setStatus('已保存。新任务使用此配置；留空的 API Key 保持不变。保存没有进行联网或付费验证。')
      window.dispatchEvent(new Event('siyi:provider-settings-changed'))
      void api<ProviderPolicy>('/api/provider/policy').then(value => { if (generation === lifetime.current) setPolicy(value) }).catch(() => {})
    } catch (error) { if (generation === lifetime.current) setStatus((error as Error).message) }
    finally { if (generation === lifetime.current) setBusy(false) }
  }

  async function discover() {
    if (!configuration || busy) return
    const generation = lifetime.current
    setBusy(true)
    setStatus('')
    setConnection(null)
    try {
      const result = await api<ProviderModelDirectory>('/api/provider/models', modelDirectoryRequest(configuration, modelKey, savedConfiguration))
      if (generation !== lifetime.current) return
      setConnection(result)
      if (result.status === 'ok') setDirectory(result.models)
    } catch (error) { if (generation === lifetime.current) setStatus((error as Error).message) }
    finally { if (generation === lifetime.current) setBusy(false) }
  }

  function addModel() {
    const id = manualModel.trim()
    if (!MODEL_ID.test(id) || id.includes('..')) { setStatus('请输入有效的模型 ID，不要填写网址、空格或密钥。'); return }
    setDirectory(items => [...items.filter(item => item.id !== id), { id, name: id }])
    update({ model: id })
    setAddingModel(false)
    setManualModel('')
  }

  return <section className="provider-card model-api-card" aria-labelledby="provider-title">
    <header className="model-api-heading"><KeyRound size={20} /><div><h3 id="provider-title">模型与 API</h3><p>选择提供商，填入密钥，再选择模型。</p></div></header>
    {!configuration ? <p className="provider-note" role="status">{status || '正在读取设置…'}</p> : <form className="model-api-form" onSubmit={save}>
      <fieldset disabled={busy}>
        <label htmlFor="model-provider">提供商</label>
        <select id="model-provider" value={configuration.provider_id} onChange={event => changeProvider(event.target.value as ProviderConfiguration['provider_id'])}>
          <option value="deepseek">DeepSeek</option>
          <option value="openai_compatible">自定义模型 API（OpenAI-compatible）</option>
          <option value="ollama">Ollama（本机）</option>
          <option value="mock">Mock（测试）</option>
        </select>

        {!local && <div className="model-api-field"><label htmlFor="deepseek-api-key">API Key</label><input id="deepseek-api-key" type="password" autoComplete="off" spellCheck={false} value={modelKey} placeholder="留空保留已保存的密钥，输入新密钥可替换" onChange={event => { setModelKey(event.target.value); setConnection(null) }} /><small>密钥只保存在 Windows 凭据管理器，不写入配置文件。</small></div>}
        {local && <p className="model-api-local-note">{isMock ? '确定性测试，不连接真实模型。' : '此页检查本机模型时不读取或发送云端 API Key，也不会自动下载模型。'}</p>}

        {!isMock && <div className="model-api-field"><div className="model-api-field-heading"><label htmlFor="provider-model">模型</label><button type="button" className="model-api-text-button" onClick={() => { void discover() }}><RefreshCw size={14} />获取可用模型</button></div>
          <select id="provider-model" value={configuration.model} onChange={event => update({ model: event.target.value })}>
            {!configuration.model && <option value="">{isOllama ? '请选择已安装模型' : '请选择模型'}</option>}
            {[...choices.values()].map(item => <option key={item.id} value={item.id}>{item.name === item.id ? item.id : `${item.name} · ${item.id}`}</option>)}
          </select>
          <small>目录获取只检查连接，不执行付费对话；模型实际对话能力仍需单独验证。</small>
          {!isOllama && <><button type="button" className="model-api-add" onClick={() => setAddingModel(value => !value)} aria-expanded={addingModel}><Plus size={14} />添加模型</button>{addingModel && <div className="model-api-add-row"><input aria-label="新模型 ID" value={manualModel} onChange={event => setManualModel(event.target.value)} placeholder="填写提供商给出的 Model ID" /><button type="button" className="secondary" onClick={addModel}>添加并选用</button></div>}</>}
        </div>}

        {!isMock && <details className="model-api-advanced"><summary><ChevronDown size={15} />高级设置</summary><div className="model-api-advanced-fields">
          <label htmlFor="compatible-endpoint">API 地址</label><input id="compatible-endpoint" type="url" value={configuration.base_url} placeholder={configuration.provider_id === 'deepseek' ? 'https://api.deepseek.com' : 'https://provider.example/v1'} onChange={event => update({ base_url: event.target.value })} /><small>填写服务根地址或 /v1，不要填写 /chat/completions。</small>
          <div className="model-api-numbers"><label>超时（秒）<input type="number" min={1} max={600} value={configuration.timeout_seconds} onChange={event => update({ timeout_seconds: Number(event.target.value) })} /></label><label>最大输出 Token<input type="number" min={isOllama ? 2048 : 1} max={1000000} value={configuration.max_tokens} onChange={event => update({ max_tokens: Number(event.target.value) })} /></label><label>失败重试次数<input type="number" min={0} max={5} value={configuration.max_retries} onChange={event => update({ max_retries: Number(event.target.value) })} /></label></div>
          <div className="model-api-toggles"><label><input type="checkbox" checked={configuration.allow_streaming} onChange={event => update({ allow_streaming: event.target.checked })} />流式输出</label><label><input type="checkbox" checked={configuration.allow_tools} onChange={event => update({ allow_tools: event.target.checked })} />工具调用</label></div>
          {configuration.provider_id === 'deepseek' && <a href="https://api-docs.deepseek.com/zh-cn/" target="_blank" rel="noreferrer">DeepSeek API 文档<ExternalLink size={12} /></a>}
        </div></details>}
      </fieldset>
      {connection && <p className={`model-api-feedback ${connection.status}`} role="status">{modelConnectionMessage(connection)}</p>}
      {status && <p className="model-api-feedback" role="status">{status}</p>}
      <footer className="model-api-actions"><button type="button" className="secondary" disabled={busy} onClick={() => { void discover() }}>{busy ? '处理中…' : '检查连接'}</button><button type="submit" className="primary" disabled={busy || (isOllama && !configuration.model.trim())}>{busy ? '处理中…' : '保存'}</button></footer>
    </form>}
  </section>
}
