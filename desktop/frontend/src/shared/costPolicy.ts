export interface CostEstimate {
  cost_status?: 'known' | 'unknown' | 'partial'
  estimated_cost_usd?: number | null
  known_cost_usd?: number
  unknown_cost_requests?: number
  pending_cost_requests?: number
}

const isAmount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0

const count = (value: unknown): number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : 0

const formatAmount = (value: number, digits: number): string => {
  const threshold = 10 ** -digits
  return value > 0 && value < threshold ? `<$${threshold.toFixed(digits)}` : `$${value.toFixed(digits)}`
}

export function costEstimateLabel(estimate: CostEstimate, digits = 4): string {
  if (estimate.cost_status !== 'known' || !isAmount(estimate.estimated_cost_usd)
    || count(estimate.unknown_cost_requests) > 0 || count(estimate.pending_cost_requests) > 0) {
    return '费用未知'
  }
  return formatAmount(estimate.estimated_cost_usd, digits)
}

export function costEstimateDetail(estimate: CostEstimate, digits = 4): string {
  if (costEstimateLabel(estimate, digits) !== '费用未知') return '仅供路由与费率估算，不是实际账单'
  const details: string[] = []
  if (estimate.cost_status === 'partial' && isAmount(estimate.known_cost_usd)) {
    details.push(`已知部分 ${formatAmount(estimate.known_cost_usd, digits)}`)
  }
  const unknown = count(estimate.unknown_cost_requests)
  const pending = count(estimate.pending_cost_requests)
  if (unknown > 0) details.push(`${unknown} 条记录费用未确认`)
  if (pending > 0) details.push(`${pending} 次尚未结算`)
  return details.join(' · ') || '模型单价或返回用量尚未确认，不能按零费用计算'
}
