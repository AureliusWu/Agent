import { useEffect, useState } from 'react'
import { Cpu, Download, Play, RefreshCw, Square, Volume2 } from 'lucide-react'
import { api, apiFetch } from '../../api'
import type { TtsSettings } from '../../hooks/useTtsPlayback'

interface ServiceState {
  status: string
  mode: string | null
  version?: string | null
  listener_pid?: number | null
  base_url?: string
}

interface LocalModel {
  name: string
  size: number
  parameter_size: string
  quantization: string
  loaded: boolean
  recommended: boolean
  context_length: number
  size_vram: number
  expires_at?: string | null
}

interface Voice { name: string; culture: string; provider: string }
interface DownloadPreview { model: string; estimated_bytes: number | null; target_directory: string }
interface DownloadState { model: string; status: string; completed: number; total: number; error?: string | null }
interface Resources { snapshot: { system_available_bytes: number | null; gpu_free_bytes: number | null; ollama_rss_bytes: number | null }; policy: { default_keep_alive: string; max_loaded_models: number } }

export function LocalAiPanel() {
  const [service, setService] = useState<ServiceState | null>(null)
  const [models, setModels] = useState<LocalModel[]>([])
  const [tts, setTts] = useState<TtsSettings | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [downloadModel, setDownloadModel] = useState('')
  const [download, setDownload] = useState<DownloadState | null>(null)
  const [resources, setResources] = useState<Resources | null>(null)
  const [keepAlive, setKeepAlive] = useState('5m')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    const [nextService, nextModels, nextTts, nextVoices, nextResources] = await Promise.all([
      api<ServiceState>('/api/local-models/service'),
      api<LocalModel[]>('/api/local-models/models').catch(() => []),
      api<TtsSettings>('/api/tts/settings'),
      api<Voice[]>('/api/tts/voices').catch(() => []),
      api<Resources>('/api/local-models/resources').catch(() => null),
    ])
    setService(nextService)
    setModels(nextModels)
    setTts(nextTts)
    setVoices(nextVoices)
    setResources(nextResources)
  }

  useEffect(() => { void load().catch(caught => setError((caught as Error).message)) }, [])
  useEffect(() => {
    if (!download || ['INSTALLED', 'CANCELLED', 'ERROR'].includes(download.status)) return
    const timer = window.setInterval(() => {
      void api<DownloadState>(`/api/local-models/download?model=${encodeURIComponent(download.model)}`).then(setDownload).catch(() => {})
    }, 750)
    return () => window.clearInterval(timer)
  }, [download])

  const action = async (name: string, work: () => Promise<unknown>) => {
    setBusy(name)
    setError('')
    try { await work(); await load() } catch (caught) { setError((caught as Error).message) } finally { setBusy('') }
  }

  const saveTts = async (change: Partial<TtsSettings>) => {
    if (!tts) return
    const updated = await api<TtsSettings>('/api/tts/settings', { method: 'PUT', body: JSON.stringify(change) })
    setTts(updated)
    window.dispatchEvent(new CustomEvent('siyi:tts-settings', { detail: updated }))
  }

  const startDownload = async () => {
    const model = downloadModel.trim()
    if (!model) return
    const preview = await api<DownloadPreview>(`/api/local-models/download/preview?model=${encodeURIComponent(model)}`)
    const size = preview.estimated_bytes ? `${(preview.estimated_bytes / 1024 / 1024 / 1024).toFixed(2)} GiB` : '未知体积'
    if (!window.confirm(`下载 ${model}？\n预计体积：${size}\n目标目录：${preview.target_directory}\n下载后不会自动加载。`)) return
    setDownload(await api<DownloadState>('/api/local-models/download', { method: 'POST', body: JSON.stringify({ model, confirmed: true }) }))
  }

  const testVoice = async () => {
    const result = await api<{ request_id: string; audio_url: string }>('/api/tts/speak', { method: 'POST', body: JSON.stringify({ text: '你好，我是夏目心。', cache: false }) })
    const response = await apiFetch(result.audio_url)
    if (!response.ok) throw new Error(`音频读取失败：HTTP ${response.status}`)
    const url = URL.createObjectURL(await response.blob())
    const audio = new Audio(url)
    await api(`/api/tts/playback/${result.request_id}/start`, { method: 'POST' })
    try {
      await audio.play()
      await new Promise<void>((resolve, reject) => { audio.onended = () => resolve(); audio.onerror = () => reject(new Error('试听播放失败')) })
      await api(`/api/tts/playback/${result.request_id}/complete`, { method: 'POST' })
    } catch (error) {
      await api(`/api/tts/playback/${result.request_id}/complete?failed=true`, { method: 'POST' }).catch(() => {})
      throw error
    } finally { URL.revokeObjectURL(url) }
  }

  return <div className="local-ai-panel">
    <h3><Cpu size={16}/> 本地模型与 Ollama</h3>
    <div className="extension-row">
      <span><Cpu /></span>
      <div><strong>{service?.status || '检测中'}</strong><p>{service?.mode || '未运行'} · {service?.version || '版本未知'} · PID {service?.listener_pid || '-'} · {service?.base_url || '127.0.0.1'}</p></div>
      <button title="刷新" onClick={() => void load()}><RefreshCw size={15}/></button>
      {service?.status === 'INSTALLED_STOPPED' && <button title="托管启动" disabled={Boolean(busy)} onClick={() => void action('start', () => api('/api/local-models/service/start', { method: 'POST', body: '{}' }))}><Play size={15}/></button>}
      {service?.status === 'MANAGED_RUNNING' && <button title="停止托管服务" disabled={Boolean(busy)} onClick={() => void action('stop', () => api('/api/local-models/service/stop', { method: 'POST' }))}><Square size={15}/></button>}
    </div>
    {models.map(model => <div className="extension-row" key={model.name}>
      <span><Download /></span>
      <div><strong>{model.name}{model.recommended ? ' · 推荐' : ''}</strong><p>{model.parameter_size} · {model.quantization} · {(model.size / 1024 / 1024 / 1024).toFixed(2)} GiB · {model.loaded ? '已加载' : '未加载'} · ctx {model.context_length || '-'} · VRAM {(model.size_vram / 1024 / 1024).toFixed(0)} MiB</p></div>
      <button title={model.loaded ? '卸载并释放内存/显存' : `预加载 ${keepAlive}`} disabled={Boolean(busy)} onClick={() => void action(model.loaded ? 'unload' : 'load', () => api(`/api/local-models/${model.loaded ? 'unload' : 'load'}`, { method: 'POST', body: JSON.stringify({ model: model.name, ...(model.loaded ? {} : { keep_alive: keepAlive }) }) }))}>{model.loaded ? <Square size={15}/> : <Play size={15}/>}</button>
    </div>)}
    <div className="local-ai-runtime"><label>keep_alive <select value={keepAlive} onChange={event => setKeepAlive(event.target.value)}><option value="0">0</option><option value="5m">5 分钟</option><option value="10m">10 分钟</option><option value="-1">常驻</option></select></label><span>可用内存 {resources?.snapshot.system_available_bytes ? (resources.snapshot.system_available_bytes / 1024 / 1024 / 1024).toFixed(1) : '-'} GiB · 可用显存 {resources?.snapshot.gpu_free_bytes ? (resources.snapshot.gpu_free_bytes / 1024 / 1024 / 1024).toFixed(1) : '-'} GiB · Ollama RSS {resources?.snapshot.ollama_rss_bytes ? (resources.snapshot.ollama_rss_bytes / 1024 / 1024).toFixed(0) : '-'} MiB</span></div>
    <div className="local-ai-download">
      <input value={downloadModel} onChange={event => setDownloadModel(event.target.value)} placeholder="输入模型，例如 qwen3:8b"/>
      <button className="secondary" disabled={Boolean(busy) || !downloadModel.trim()} onClick={() => void startDownload()}><Download size={15}/>确认后下载</button>
    </div>
    {download && <div className="local-ai-download-status"><span>{download.model} · {download.status}</span><progress value={download.completed} max={download.total || 1}/>{download.status === 'DOWNLOADING' && <button onClick={() => void api<DownloadState>('/api/local-models/download/cancel', { method: 'POST', body: JSON.stringify({ model: download.model }) }).then(setDownload)}>取消</button>}</div>}

    <h3><Volume2 size={16}/> 本地语音</h3>
    {tts && <div className="local-ai-controls">
      <label><input type="checkbox" checked={tts.enabled} onChange={event => void saveTts({ enabled: event.target.checked })}/> 启用 TTS</label>
      <label>首选 Provider<select value={tts.provider} onChange={event => void saveTts({ provider: event.target.value })}><option value="melotts">MeloTTS CPU</option><option value="windows">Windows 系统语音</option></select></label>
      <label>音色<select value={tts.voice} onChange={event => void saveTts({ voice: event.target.value })}><option value="">Provider 默认</option>{voices.filter(item => item.provider === tts.provider || item.provider === tts.fallback_provider).map(item => <option key={`${item.provider}:${item.name}`} value={item.name}>{item.name} · {item.culture}</option>)}</select></label>
      <label>播放模式<select value={tts.playback_mode} onChange={event => void saveTts({ playback_mode: event.target.value })}><option value="MANUAL">手动</option><option value="AUTO">流式自动播放</option><option value="SYSTEM_ONLY">仅系统提示</option><option value="OFF">关闭</option></select></label>
      <label>语速<input type="range" min="0.5" max="2" step="0.1" value={tts.speed} onChange={event => void saveTts({ speed: Number(event.target.value) })}/><span>{tts.speed.toFixed(1)}×</span></label>
      <label><input type="checkbox" checked={tts.cache_enabled} onChange={event => void saveTts({ cache_enabled: event.target.checked })}/> 音频缓存（敏感文本始终不缓存）</label>
      <button className="secondary" disabled={Boolean(busy)} onClick={() => void action('voice-test', testVoice)}><Volume2 size={15}/>测试中文语音</button>
    </div>}
    {error && <p className="extension-error">{error}</p>}
  </div>
}
