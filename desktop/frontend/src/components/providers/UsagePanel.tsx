import { useEffect, useState } from 'react'
import { Activity, Coins, Gauge, Network } from 'lucide-react'
import { api } from '../../api'
import { PanelHeader } from '../shared/PanelHeader'
import { costEstimateDetail, costEstimateLabel } from '../../shared/costPolicy'
import type { CostEstimate } from '../../shared/costPolicy'

interface UsageSummary {
  period_days: number
  totals: CostEstimate & { requests: number; input_tokens: number; output_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; cache_write_tokens: number; cache_hit_rate: number; total_tokens: number; average_duration_ms: number }
  models: Array<CostEstimate & { provider: string; model: string; requests: number; total_tokens: number; cached_input_tokens: number; uncached_input_tokens: number }>
  daily: Array<{ date: string; requests: number; input_tokens: number; output_tokens: number; cached_input_tokens: number; uncached_input_tokens: number; total_tokens: number }>
}

export function UsagePanel() {
  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { api<UsageSummary>('/api/usage/summary?days=30').then(setSummary).catch(caught => setError((caught as Error).message)) }, [])
  return <section className="content-panel usage-panel">
    <PanelHeader icon={<Activity />} title="用量统计" subtitle="基于每次模型请求返回的真实 Token 用量聚合" />
    {error && <p className="panel-error">{error}</p>}
    {summary && <>
      <div className="usage-cards">
        <article><Gauge /><small>总 Token</small><strong>{summary.totals.total_tokens.toLocaleString()}</strong><span>输入 {summary.totals.input_tokens.toLocaleString()} · 输出 {summary.totals.output_tokens.toLocaleString()}</span></article>
        <article><Network /><small>缓存命中率</small><strong>{(summary.totals.cache_hit_rate * 100).toFixed(1)}%</strong><span>命中 {summary.totals.cached_input_tokens.toLocaleString()} · 未缓存 {summary.totals.uncached_input_tokens.toLocaleString()}</span></article>
        <article><Network /><small>模型请求</small><strong>{summary.totals.requests.toLocaleString()}</strong><span>平均 {Math.round(summary.totals.average_duration_ms).toLocaleString()} ms</span></article>
        <article><Coins /><small>预估成本</small><strong>{costEstimateLabel(summary.totals)}</strong><span>{costEstimateDetail(summary.totals)}</span></article>
      </div>
      <div className="usage-section"><h3>按模型</h3>{summary.models.map(item => <div className="usage-row" key={`${item.provider}:${item.model}`}><strong>{item.model}</strong><span>{item.provider}</span><code>{item.total_tokens.toLocaleString()} tokens</code><small>缓存 {item.cached_input_tokens.toLocaleString()} · {item.requests} 次</small></div>)}</div>
      <div className="usage-section"><h3>近 {summary.period_days} 天</h3>{summary.daily.length ? summary.daily.map(item => <div className="usage-row" key={item.date}><strong>{item.date}</strong><span>输入 {item.input_tokens.toLocaleString()} · 缓存 {item.cached_input_tokens.toLocaleString()}</span><code>{item.total_tokens.toLocaleString()} tokens</code><small>{item.requests} 次</small></div>) : <div className="empty-panel">暂无用量记录</div>}</div>
    </>}
  </section>
}
