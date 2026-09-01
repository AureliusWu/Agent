export type FileAction = 'create' | 'edit' | 'rename' | 'move' | 'copy' | 'delete'
export interface FileOperation { operation: string; arguments: Record<string, unknown> }
export interface FileToolResult {
  status?: string
  success?: boolean
  approval_key?: string
  error_code?: string
  error_message?: string
  error?: string
  [key: string]: unknown
}

export const FILE_ACTION_LABELS: Record<FileAction, string> = { create: '新建', edit: '编辑', rename: '重命名', move: '移动', copy: '复制', delete: '删除' }

function relativeFilePath(value: string): string {
  const path = value.trim().replaceAll('\\', '/')
  if (!path || path === '.' || path.startsWith('/') || path.includes(':') || path.split('/').some(part => !part || part === '..' || part === '.')) {
    throw new Error('请输入工作区内明确的相对文件路径')
  }
  return path
}

export function buildFileOperation(action: FileAction, source: string, destination: string, content: string, versionToken?: string): FileOperation {
  const path = relativeFilePath(source)
  if (action === 'create') return { operation: 'file.write', arguments: { path, content, expected_version_token: 'missing' } }
  if (!versionToken) throw new Error('缺少文件版本，请重新读取文件后再预览')
  if (action === 'edit') return { operation: 'file.write', arguments: { path, content, expected_version_token: versionToken } }
  if (action === 'delete') return { operation: 'file.delete', arguments: { path, expected_version_token: versionToken } }
  const target = relativeFilePath(destination)
  if (target.toLowerCase() === path.toLowerCase()) throw new Error('源路径与目标路径必须不同')
  return { operation: `file.${action}`, arguments: { source: path, destination: target, expected_version_token: versionToken, expected_destination_version_token: 'missing' } }
}

export function canConfirmFileOperation(mode: string, result: FileToolResult): boolean {
  return mode !== 'readonly' && result.status === 'confirmation_required' && typeof result.approval_key === 'string' && result.approval_key.length > 0
}

export function fileOperationError(result: FileToolResult): string {
  if (JSON.stringify(result).includes('version_conflict')) return '文件版本冲突：未自动覆盖。请重新读取文件、检查差异并重新预览计划。'
  return String(result.error_message || result.error || (result.status === 'confirmation_required' ? '授权尚未完成；未继续执行，请重新预览计划' : '操作未完成，请检查操作结果'))
}

export function deletionSummary(operations: FileOperation[]): string {
  const targets = operations.filter(item => item.operation === 'file.delete').map(item => String(item.arguments.path))
  return targets.length ? `删除目标：${targets.join('、')}\n数量：${targets.length} 个文件\n恢复：成功时由核心创建可撤销备份；备份被删除或文件随后变更会影响恢复。` : ''
}
