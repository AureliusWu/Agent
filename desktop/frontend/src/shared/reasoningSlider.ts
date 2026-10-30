import type { ProviderProfile, ReasoningEffort } from '../types'

export type ReasoningProvider = Pick<ProviderProfile, 'id' | 'capabilities'>

// Keep the public chat contract, not the larger transport-only vocabulary.
export const REASONING_STEPS: readonly ReasoningEffort[] = ['auto', 'low', 'medium', 'high']

export function reasoningSliderIndex(value: ReasoningEffort): number {
  const index = REASONING_STEPS.indexOf(value)
  return index < 0 ? 0 : index
}

export function reasoningFromSliderIndex(value: string | number): ReasoningEffort | null {
  if (value === '') return null
  const index = Number(value)
  return Number.isInteger(index) && index >= 0 && index < REASONING_STEPS.length ? REASONING_STEPS[index] : null
}

export function reasoningSliderKeyValue(key: string, current: ReasoningEffort): ReasoningEffort | null {
  const index = reasoningSliderIndex(current)
  if (key === 'Home') return REASONING_STEPS[0]
  if (key === 'End') return REASONING_STEPS[REASONING_STEPS.length - 1]
  if (key === 'ArrowRight' || key === 'ArrowUp') return REASONING_STEPS[Math.min(index + 1, REASONING_STEPS.length - 1)]
  if (key === 'ArrowLeft' || key === 'ArrowDown') return REASONING_STEPS[Math.max(index - 1, 0)]
  return null
}

export function reasoningExplicitlyUnsupported(provider?: ReasoningProvider): boolean {
  return provider?.capabilities?.reasoning === false || provider?.capabilities?.supports_reasoning === false
}

export function reasoningSliderDescription(value: ReasoningEffort, provider?: ReasoningProvider): string {
  if (reasoningExplicitlyUnsupported(provider)) return '当前提供商未提供推理强度控制。其他模型的已选偏好会保留；当前模型不应用此设置。'
  if (provider?.id === 'deepseek') {
    if (value === 'auto') return '自动使用司忆现有路由策略。手动档位不是速度承诺；中推理按 DeepSeek High 执行。'
    if (value === 'medium') return '兼容档位：中推理在当前 DeepSeek 接口中映射为 High，与高推理使用相同参数。'
    return value === 'low' ? '向 DeepSeek 请求 Low 推理。实际响应时间和推理量由模型决定。' : '向 DeepSeek 请求 High 推理。实际响应时间和推理量由模型决定。'
  }
  return value === 'auto'
    ? '自动使用司忆现有路由策略。手动档位是否生效取决于当前提供商与模型。'
    : '已选择推理偏好；当前提供商与模型的支持情况未确认，兼容接口不保证应用此档位。'
}
