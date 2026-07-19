import { Gauge, Sparkles } from 'lucide-react'
import { REASONING_LABEL } from '../constants'
import type { ReasoningEffort } from '../types'

interface Props {
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  reasoningEffort: ReasoningEffort
  onPreferredModel: (value: string) => void
  onReasoningEffort: (value: ReasoningEffort) => void
}

export function ModelReasoningMenu(props: Props) {
  return <div className="composer-popover model-menu" role="dialog" aria-label="模型与推理">
    <header><Sparkles size={16} /><strong>模型与推理</strong></header>
    <label>当前模型
      <select value={props.preferredModel} onChange={event => props.onPreferredModel(event.target.value)}>
        <option value="">自动路由（{props.defaultModel}）</option>
        {props.modelOptions.map(model => <option value={model} key={model}>{model}</option>)}
      </select>
    </label>
    <fieldset>
      <legend>推理强度</legend>
      <div className="reasoning-grid">
        {(['auto', 'low', 'medium', 'high'] as ReasoningEffort[]).map(value => <button type="button" className={props.reasoningEffort === value ? 'active' : ''} key={value} onClick={() => props.onReasoningEffort(value)}>{REASONING_LABEL[value]}</button>)}
      </div>
    </fieldset>
    <div className="speed-row" aria-disabled="true"><Gauge size={16} /><span><strong>标准速度</strong><small>当前 Provider 未提供速度切换</small></span></div>
  </div>
}
