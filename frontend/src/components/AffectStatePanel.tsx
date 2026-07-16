import { useEffect, useState } from 'react'
import { Activity, HeartHandshake } from 'lucide-react'
import { api } from '../api'

interface AgentState {
  affect: { mood: Record<string, number>; emotion: { type: string; intensity: number } }
  relationship: { state: Record<string, number>; shared_history_count: number }
}

const labels: Record<string, string> = {
  valence: '情绪倾向', energy: '精力', security: '安全感', confidence: '信心',
  trust: '信任', familiarity: '熟悉', collaboration_depth: '协作深度', recent_tension: '近期张力',
}

export function AffectStatePanel() {
  const [state, setState] = useState<AgentState | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { void api<AgentState>('/api/state').then(setState).catch(caught => setError((caught as Error).message)) }, [])
  if (error) return <p className="panel-error">状态读取失败：{error}</p>
  if (!state) return <div className="state-summary">状态加载中…</div>
  const mood = ['valence', 'energy', 'security', 'confidence']
  const relationship = ['trust', 'familiarity', 'collaboration_depth', 'recent_tension']
  return <section className="state-summary" aria-label="夏目心状态">
    <div><header><Activity size={16} /><strong>当前心境</strong><span>{state.affect.emotion.type} · {Math.round(state.affect.emotion.intensity * 100)}%</span></header>{mood.map(key => <Meter key={key} label={labels[key]} value={state.affect.mood[key]} signed={key === 'valence'} />)}</div>
    <div><header><HeartHandshake size={16} /><strong>关系状态</strong><span>共同记录 {state.relationship.shared_history_count}</span></header>{relationship.map(key => <Meter key={key} label={labels[key]} value={state.relationship.state[key]} />)}</div>
    <p>状态只影响语气与关注方式，不改变事实、安全、权限或身份。</p>
  </section>
}

function Meter({ label, value = 0, signed = false }: { label: string; value?: number; signed?: boolean }) {
  const normalized = signed ? (value + 1) / 2 : value
  return <div className="state-meter"><span>{label}</span><progress max="1" value={Math.max(0, Math.min(1, normalized))} /><output>{Math.round(value * 100)}%</output></div>
}
