export type QualificationLevelName = 'basic' | 'readonly_tools' | 'structured_plan' | 'file_agent'
export interface QualificationLevel { qualified: boolean; status: string; reasons: string[] }
export interface ModelQualification {
  effective_capabilities: { model: string; context_window_tokens: number | null; window_status: string; model_digest: string | null }
  qualification: { status: string; protocol: string; target_version: string; run_id: string | null; finished_at: string | null; trust: string; levels: Record<QualificationLevelName, QualificationLevel> }
}
export const QUALIFICATION_LEVELS: [QualificationLevelName, string][] = [
  ['basic', '基础对话'], ['readonly_tools', '只读工具'], ['structured_plan', '结构化计划'], ['file_agent', '文件 Agent'],
]
export const MAX_QUALIFICATION_BYTES = 512 * 1024

export function qualificationLabel(level: QualificationLevel | undefined): string {
  if (!level) return '状态未知'
  if (level.status === 'PASS' && level.qualified === true) return '已通过当前模型验收'
  if (level.status === 'STALE') return '已失效，需重新验收'
  if (level.status === 'NOT_APPLICABLE') return '不适用'
  if (level.status === 'NOT_RUN') return '尚未验收'
  if (level.status === 'INVALID') return '证据无效'
  if (level.status === 'FAIL') return '未通过'
  return '状态未知'
}
