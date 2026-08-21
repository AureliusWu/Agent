import { useEffect, useState } from 'react'
import { Cpu, Download, Mic, Play, RefreshCw, Square, Trash2, Volume2 } from 'lucide-react'
import { api, apiFetch } from '../../api'
import type { TtsSettings } from '../../hooks/useTtsPlayback'
import {
  createPlaybackInterruptionGate,
  createPlaybackSettlement,
  waitForPlayback,
} from '../../ttsPlaybackSettlement'
import { MicrophoneSettingsControl } from './MicrophoneSettingsControl'

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

interface SttSettings {
  enabled: boolean
  provider: string
  model_id: string
  device: 'cpu' | 'cuda'
  compute_type: string
  vad: boolean
  idle_unload_minutes: number
  gpu_experimental: boolean
}

interface SttModel {
  id: string
  provider: string
  repo_id: string
  estimated_bytes: number
  description: string
  default_for_new_install: boolean
  installed: boolean
  status: 'MODEL_MISSING' | 'INSTALLED' | 'LOADED'
  size_bytes: number
  storage_path: string
  loaded: boolean
}

interface SttProviderHealth { name?: string; status: string; message?: string; version?: string | null }
interface SttHealth { status: string; providers: SttProviderHealth[]; worker: { loaded_model: string | null; pid: number | null } }
interface SttStatus { status: string; loaded_model: string | null; worker_pid: number | null }
interface SttDownloadPreview {
  model: string
  repo_id: string
  estimated_bytes: number
  target_directory: string
  already_installed: boolean
  available_bytes: number
  required_bytes: number
  fits: boolean
}
interface SttDownloadState { model: string; status: string; completed: number; total: number; error?: string | null }

function formatBytes(value: number | null | undefined): string {
  if (!value || value <= 0) return '0 B'
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KiB`
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(0)} MiB`
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GiB`
}

function sttStatusLabel(status: SttModel['status']): string {
  if (status === 'LOADED') return '已加载'
  if (status === 'INSTALLED') return '已安装'
  return '未下载'
}

function sttDownloadFinished(status: string): boolean {
  return ['INSTALLED', 'CANCELLED', 'ERROR', 'NOT_STARTED'].includes(status)
}

export function LocalAiPanel() {
  const [service, setService] = useState<ServiceState | null>(null)
  const [models, setModels] = useState<LocalModel[]>([])
  const [tts, setTts] = useState<TtsSettings | null>(null)
  const [voices, setVoices] = useState<Voice[]>([])
  const [downloadModel, setDownloadModel] = useState('')
  const [download, setDownload] = useState<DownloadState | null>(null)
  const [resources, setResources] = useState<Resources | null>(null)
  const [sttSettings, setSttSettings] = useState<SttSettings | null>(null)
  const [sttModels, setSttModels] = useState<SttModel[]>([])
  const [sttHealth, setSttHealth] = useState<SttHealth | null>(null)
  const [sttStatus, setSttStatus] = useState<SttStatus | null>(null)
  const [sttDownloads, setSttDownloads] = useState<Record<string, SttDownloadState>>({})
  const [keepAlive, setKeepAlive] = useState('5m')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    const [nextService, nextModels, nextTts, nextVoices, nextResources, nextSttSettings, nextSttModels, nextSttHealth, nextSttStatus] = await Promise.all([
      api<ServiceState>('/api/local-models/service'),
      api<LocalModel[]>('/api/local-models/models').catch(() => []),
      api<TtsSettings>('/api/tts/settings'),
      api<Voice[]>('/api/tts/voices').catch(() => []),
      api<Resources>('/api/local-models/resources').catch(() => null),
      api<SttSettings>('/api/stt/settings').catch(() => null),
      api<SttModel[]>('/api/stt/models').catch(() => []),
      api<SttHealth>('/api/stt/health').catch(() => null),
      api<SttStatus>('/api/stt/status').catch(() => null),
    ])
    setService(nextService)
    setModels(nextModels)
    setTts(nextTts)
    setVoices(nextVoices)
    setResources(nextResources)
    setSttSettings(nextSttSettings)
    setSttModels(nextSttModels)
    setSttHealth(nextSttHealth)
    setSttStatus(nextSttStatus)
    const nextSttDownloads = await Promise.all(nextSttModels.map(async model => {
      try { return await api<SttDownloadState>(`/api/stt/models/${encodeURIComponent(model.id)}/download`) } catch { return null }
    }))
    setSttDownloads(current => {
      const next = { ...current }
      for (const state of nextSttDownloads) if (state) next[state.model] = state
      return next
    })
  }

  useEffect(() => { void load().catch(caught => setError((caught as Error).message)) }, [])
  useEffect(() => {
    if (!download || ['INSTALLED', 'CANCELLED', 'ERROR'].includes(download.status)) return
    const timer = window.setInterval(() => {
      void api<DownloadState>(`/api/local-models/download?model=${encodeURIComponent(download.model)}`).then(setDownload).catch(() => {})
    }, 750)
    return () => window.clearInterval(timer)
  }, [download])
  const activeSttDownloadKey = Object.values(sttDownloads)
    .filter(item => !sttDownloadFinished(item.status))
    .map(item => item.model)
    .sort()
    .join('|')
  useEffect(() => {
    const activeSttDownloadIds = activeSttDownloadKey ? activeSttDownloadKey.split('|') : []
    if (!activeSttDownloadIds.length) return
    let disposed = false
    const refreshDownloads = async () => {
      const values = await Promise.all(activeSttDownloadIds.map(async model => {
        try { return await api<SttDownloadState>(`/api/stt/models/${encodeURIComponent(model)}/download`) } catch { return null }
      }))
      if (disposed) return
      const resolved = values.filter((value): value is SttDownloadState => Boolean(value))
      if (!resolved.length) return
      setSttDownloads(current => ({ ...current, ...Object.fromEntries(resolved.map(value => [value.model, value])) }))
      if (resolved.some(value => sttDownloadFinished(value.status))) void load().catch(caught => setError((caught as Error).message))
    }
    void refreshDownloads()
    const timer = window.setInterval(() => { void refreshDownloads() }, 750)
    return () => {
      disposed = true
      window.clearInterval(timer)
    }
  }, [activeSttDownloadKey])

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

  const saveStt = async (change: Partial<SttSettings>) => {
    if (!sttSettings) return
    setBusy('stt-settings')
    setError('')
    try {
      setSttSettings(await api<SttSettings>('/api/stt/settings', { method: 'PUT', body: JSON.stringify(change) }))
      await load()
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy('')
    }
  }

  const startDownload = async () => {
    const model = downloadModel.trim()
    if (!model) return
    const preview = await api<DownloadPreview>(`/api/local-models/download/preview?model=${encodeURIComponent(model)}`)
    const size = preview.estimated_bytes ? `${(preview.estimated_bytes / 1024 / 1024 / 1024).toFixed(2)} GiB` : '未知体积'
    if (!window.confirm(`下载 ${model}？\n预计体积：${size}\n目标目录：${preview.target_directory}\n下载后不会自动加载。`)) return
    setDownload(await api<DownloadState>('/api/local-models/download', { method: 'POST', body: JSON.stringify({ model, confirmed: true }) }))
  }

  const startSttDownload = async (model: SttModel) => {
    setBusy(`stt-download-${model.id}`)
    setError('')
    try {
      const preview = await api<SttDownloadPreview>(`/api/stt/models/${encodeURIComponent(model.id)}/download-preview`)
      if (preview.already_installed) {
        setError(`${model.id} 已安装在本地；无需再次下载。`)
        return
      }
      const confirmation = [
        `下载本地语音识别模型 ${model.id}？`,
        `来源：${preview.repo_id}`,
        `模型预计体积：${formatBytes(preview.estimated_bytes)}`,
        `下载所需磁盘空间：${formatBytes(preview.required_bytes)}（可用 ${formatBytes(preview.available_bytes)}）`,
        `目标目录：${preview.target_directory}`,
        '下载后不会自动加载或启用。',
      ].join('\n')
      if (!preview.fits) {
        setError(`磁盘空间不足，无法下载 ${model.id}。`)
        return
      }
      if (!window.confirm(confirmation)) return
      const state = await api<SttDownloadState>('/api/stt/models/download', { method: 'POST', body: JSON.stringify({ model_id: model.id, confirmed: true }) })
      setSttDownloads(current => ({ ...current, [model.id]: state }))
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy('')
    }
  }

  const cancelSttDownload = async (modelId: string) => {
    setBusy(`stt-download-${modelId}`)
    setError('')
    try {
      const state = await api<SttDownloadState>('/api/stt/models/download/cancel', { method: 'POST', body: JSON.stringify({ model_id: modelId }) })
      setSttDownloads(current => ({ ...current, [modelId]: state }))
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy('')
    }
  }

  const deleteSttModel = async (model: SttModel) => {
    if (!window.confirm(`删除本地语音识别模型 ${model.id}？\n目录：${model.storage_path}\n此操作会释放 ${formatBytes(model.size_bytes)} 磁盘空间。`)) return
    setBusy(`stt-delete-${model.id}`)
    setError('')
    try {
      await api(`/api/stt/models/${encodeURIComponent(model.id)}?confirmed=true`, { method: 'DELETE' })
      setSttDownloads(current => ({ ...current, [model.id]: { model: model.id, status: 'NOT_STARTED', completed: 0, total: 0 } }))
      await load()
    } catch (caught) {
      setError((caught as Error).message)
    } finally {
      setBusy('')
    }
  }

  const testVoice = async () => {
    const settlement = createPlaybackSettlement()
    let audio: HTMLAudioElement | null = null
    let url: string | null = null
    let waitingForPlayback = false
    const interruption = createPlaybackInterruptionGate(() => {
      audio?.pause()
      if (audio) audio.currentTime = 0
      // Do not reject the settlement before waitForPlayback observes it: an
      // interrupt while /speak or the WAV fetch is pending is handled by the
      // gate checks below. Once playback is waiting, it must settle promptly.
      if (waitingForPlayback) settlement.interrupt()
    })
    // Register before the first await. The shared voice stop policy may fire
    // while synthesis or the WAV download is still pending.
    window.addEventListener('siyi:tts-interrupt', interruption.interrupt)
    let requestId: string | null = null
    try {
      const result = await api<{ request_id: string; audio_url: string }>('/api/tts/speak', { method: 'POST', body: JSON.stringify({ text: '你好，我是夏目心。', cache: false }) })
      requestId = result.request_id
      interruption.throwIfInterrupted()
      const response = await apiFetch(result.audio_url)
      if (!response.ok) throw new Error(`音频读取失败：HTTP ${response.status}`)
      url = URL.createObjectURL(await response.blob())
      interruption.throwIfInterrupted()
      const testAudio = new Audio(url)
      audio = testAudio
      testAudio.onended = settlement.complete
      testAudio.onerror = () => settlement.fail(new Error('试听播放失败'))
      await api(`/api/tts/playback/${requestId}/start`, { method: 'POST' })
      interruption.throwIfInterrupted()
      waitingForPlayback = true
      await waitForPlayback(settlement, () => testAudio.play())
      waitingForPlayback = false
      interruption.throwIfInterrupted()
      await api(`/api/tts/playback/${requestId}/complete`, { method: 'POST' })
    } catch (error) {
      if (requestId) await api(`/api/tts/playback/${requestId}/complete?failed=true`, { method: 'POST' }).catch(() => {})
      throw error
    } finally {
      waitingForPlayback = false
      window.removeEventListener('siyi:tts-interrupt', interruption.interrupt)
      if (audio) {
        audio.onended = null
        audio.onerror = null
        audio.pause()
        audio.src = ''
      }
      if (url) URL.revokeObjectURL(url)
    }
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

    <h3><Mic size={16}/> 本地语音输入（STT）</h3>
    <div className="extension-row">
      <span><Mic /></span>
      <div>
        <strong>{sttSettings?.provider || 'STT Provider 未配置'}</strong>
        <p>{sttSettings?.device === 'cuda' ? 'CUDA（实验性）' : 'CPU（默认、稳定）'} · {sttSettings?.compute_type || 'int8'} · {sttStatus?.status || 'IDLE'} · 工作进程 {sttStatus?.worker_pid || sttHealth?.worker.pid || '-'}</p>
      </div>
      <button title="刷新本地语音输入状态" onClick={() => void load()}><RefreshCw size={15}/></button>
    </div>
    {sttHealth?.providers.map(provider => <p className="local-ai-runtime" key={provider.name || provider.status}>{provider.name || 'STT Provider'} · {provider.status}{provider.version ? ` · ${provider.version}` : ''}{provider.message ? ` · ${provider.message}` : ''}</p>)}
    {sttSettings && <div className="local-ai-controls">
      <label><input type="checkbox" checked={sttSettings.enabled} disabled={Boolean(busy)} onChange={event => void saveStt({ enabled: event.target.checked })}/> 启用本地语音转写</label>
      <label>默认模型<select value={sttSettings.model_id} disabled={Boolean(busy)} onChange={event => void saveStt({ model_id: event.target.value })}>{sttModels.map(model => <option key={model.id} value={model.id}>{model.id}{model.default_for_new_install ? '（新安装推荐）' : ''} · {formatBytes(model.estimated_bytes)}</option>)}</select></label>
      <label>推理设备<select value={sttSettings.device} disabled={Boolean(busy)} onChange={event => void saveStt({ device: event.target.value as SttSettings['device'] })}><option value="cpu">CPU（默认、稳定）</option><option value="cuda" disabled={!sttSettings.gpu_experimental}>CUDA（实验性）</option></select></label>
      <label><input type="checkbox" checked={sttSettings.gpu_experimental} disabled={Boolean(busy)} onChange={event => void saveStt(event.target.checked ? { gpu_experimental: true } : { gpu_experimental: false, device: 'cpu', compute_type: 'int8' })}/> 允许 GPU 实验性模式</label>
      <label>空闲后卸载<select value={String(sttSettings.idle_unload_minutes)} disabled={Boolean(busy)} onChange={event => void saveStt({ idle_unload_minutes: Number(event.target.value) })}><option value="0">不自动卸载</option><option value="5">5 分钟</option><option value="10">10 分钟</option><option value="30">30 分钟</option></select></label>
      <label><input type="checkbox" checked={sttSettings.vad} disabled={Boolean(busy)} onChange={event => void saveStt({ vad: event.target.checked })}/> 启用静音检测（VAD）</label>
    </div>}
    {sttSettings && <MicrophoneSettingsControl
      provider={sttSettings.provider}
      model={sttSettings.model_id}
      device={sttSettings.device}
      computeType={sttSettings.compute_type}
    />}
    {sttModels.map(model => {
      const state = sttDownloads[model.id]
      const isDownloading = Boolean(state && !sttDownloadFinished(state.status))
      return <div key={model.id}>
        <div className="extension-row">
          <span><Download /></span>
          <div><strong>{model.id}{sttSettings?.model_id === model.id ? ' · 当前默认' : model.default_for_new_install ? ' · 新安装推荐' : ''}</strong><p>{model.description} · {sttStatusLabel(model.status)} · 已用 {formatBytes(model.size_bytes)} / 预计 {formatBytes(model.estimated_bytes)}</p></div>
          {!model.installed && <button title="查看体积并确认下载" disabled={Boolean(busy) || isDownloading} onClick={() => void startSttDownload(model)}><Download size={15}/></button>}
          {model.installed && !model.loaded && <button title="预加载模型" disabled={Boolean(busy)} onClick={() => void action(`stt-load-${model.id}`, () => api('/api/stt/models/load', { method: 'POST', body: JSON.stringify({ model_id: model.id }) }))}><Play size={15}/></button>}
          {model.loaded && <button title="卸载模型并释放内存" disabled={Boolean(busy)} onClick={() => void action(`stt-unload-${model.id}`, () => api('/api/stt/models/unload', { method: 'POST', body: JSON.stringify({ model_id: model.id }) }))}><Square size={15}/></button>}
          {model.installed && <button title="删除本地模型" disabled={Boolean(busy) || isDownloading} onClick={() => void deleteSttModel(model)}><Trash2 size={15}/></button>}
        </div>
        {state && <div className="local-ai-download-status"><span>{model.id} · {state.status}{state.error ? ` · ${state.error}` : ''}</span><progress value={state.completed} max={state.total || 1}/>{isDownloading && <button disabled={Boolean(busy)} onClick={() => void cancelSttDownload(model.id)}>取消</button>}</div>}
      </div>
    })}

    <h3><Volume2 size={16}/> 本地语音</h3>
    {tts && <div className="local-ai-controls">
      <label><input type="checkbox" checked={tts.enabled} onChange={event => void saveTts({ enabled: event.target.checked })}/> 启用 TTS</label>
      <label>首选 Provider<select value={tts.provider} onChange={event => void saveTts({ provider: event.target.value })}><option value="windows">Windows 系统语音（稳定）</option><option value="melotts">MeloTTS CPU（实验性）</option></select></label>
      <label>音色<select value={tts.voice} onChange={event => void saveTts({ voice: event.target.value })}><option value="">Provider 默认</option>{voices.filter(item => item.provider === tts.provider || item.provider === tts.fallback_provider).map(item => <option key={`${item.provider}:${item.name}`} value={item.name}>{item.name} · {item.culture}</option>)}</select></label>
      <label>播放模式<select value={tts.playback_mode} onChange={event => void saveTts({ playback_mode: event.target.value })}><option value="MANUAL">手动</option><option value="AUTO">流式自动播放</option><option value="SYSTEM_ONLY">仅系统提示</option><option value="OFF">关闭</option></select></label>
      <label>语速<input type="range" min="0.5" max="2" step="0.1" value={tts.speed} onChange={event => void saveTts({ speed: Number(event.target.value) })}/><span>{tts.speed.toFixed(1)}×</span></label>
      <label>音量<input type="range" min="0" max="1" step="0.05" value={tts.volume} onChange={event => void saveTts({ volume: Number(event.target.value) })}/><span>{Math.round(tts.volume * 100)}%</span></label>
      <label><input type="checkbox" checked={tts.cache_enabled} onChange={event => void saveTts({ cache_enabled: event.target.checked })}/> 音频缓存（敏感文本始终不缓存）</label>
      <button className="secondary" disabled={Boolean(busy)} onClick={() => void action('voice-test', testVoice)}><Volume2 size={15}/>测试中文语音</button>
    </div>}
    {error && <p className="extension-error">{error}</p>}
  </div>
}
