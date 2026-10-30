import { useId } from 'react'
import { Gauge, RotateCcw, Sparkles, Zap } from 'lucide-react'
import { REASONING_LABEL } from '../../constants'
import { REASONING_STEPS, reasoningExplicitlyUnsupported, reasoningFromSliderIndex, reasoningSliderDescription, reasoningSliderIndex, reasoningSliderKeyValue } from '../../shared/reasoningSlider'
import type { ReasoningProvider } from '../../shared/reasoningSlider'
import type { ReasoningEffort } from '../../types'
import './ModelReasoningMenu.css'

interface Props {
  preferredModel: string
  defaultModel: string
  modelOptions: string[]
  reasoningEffort: ReasoningEffort
  reasoningProvider?: ReasoningProvider
  busy?: boolean
  onPreferredModel: (value: string) => void
  onReasoningEffort: (value: ReasoningEffort) => void
}

export function ModelReasoningMenu(props: Props) {
  const descriptionId = useId()
  const unsupported = reasoningExplicitlyUnsupported(props.reasoningProvider)
  const disabled = Boolean(props.busy || unsupported)
  const model = props.preferredModel || props.defaultModel || '自动路由'
  const selected = unsupported ? 'auto' : props.reasoningEffort
  return <div className="composer-popover model-menu reasoning-menu" role="dialog" aria-label="模型与推理">
    <header><Sparkles size={16} /><strong>模型与推理</strong></header>
    <label>当前模型
      <select value={props.preferredModel} disabled={props.busy} onChange={event => { if (!props.busy) props.onPreferredModel(event.target.value) }}>
        <option value="">自动路由（{props.defaultModel}）</option>
        {props.modelOptions.map(model => <option value={model} key={model}>{model}</option>)}
      </select>
    </label>
    <fieldset className="reasoning-slider-card">
      <legend>推理强度</legend>
      <div className="reasoning-slider-heading">
        <Zap className="reasoning-slider-symbol" size={16} aria-hidden="true" />
        <div className="reasoning-slider-current"><strong>{REASONING_LABEL[selected]}</strong><span title={model}>{model}</span></div>
        <button type="button" className="reasoning-slider-reset" aria-label="重置为自动推理" title="重置为自动推理" disabled={disabled || selected === 'auto'} onClick={() => { if (!disabled) props.onReasoningEffort('auto') }}><RotateCcw size={16} aria-hidden="true" /></button>
      </div>
      <input className="reasoning-slider-input" type="range" min={0} max={REASONING_STEPS.length - 1} step={1} value={reasoningSliderIndex(selected)} disabled={disabled} aria-label="推理强度" aria-valuetext={REASONING_LABEL[selected]} aria-describedby={descriptionId} onChange={event => { const next = reasoningFromSliderIndex(event.target.value); if (!disabled && next !== null) props.onReasoningEffort(next) }} onKeyDown={event => { const next = reasoningSliderKeyValue(event.key, selected); if (!disabled && next !== null && !event.altKey && !event.ctrlKey && !event.metaKey) { event.preventDefault(); props.onReasoningEffort(next) } }} />
      <div className="reasoning-slider-ticks" aria-hidden="true">{REASONING_STEPS.map(value => <span className={selected === value ? 'is-selected' : ''} key={value}>{value === 'auto' ? '自动' : REASONING_LABEL[value].replace('推理', '')}</span>)}</div>
      <p className="reasoning-slider-description" id={descriptionId}>{reasoningSliderDescription(props.reasoningEffort, props.reasoningProvider)}</p>
    </fieldset>
    <div className="speed-row" aria-disabled="true"><Gauge size={16} /><span><strong>标准速度</strong><small>当前 Provider 未提供速度切换</small></span></div>
  </div>
}
