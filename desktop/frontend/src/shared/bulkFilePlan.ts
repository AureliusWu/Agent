import { buildFileOperation } from './fileOperationPolicy.ts'
import type { FileOperation } from './fileOperationPolicy'

export const MAX_BULK_FILES = 50
export const MAX_BULK_TEXT_BYTES = 1_048_576
export const MAX_BULK_TOTAL_BYTES = 8_388_608
export interface BulkFileSource { path: string; version_token: string; content?: string; truncated?: boolean }
export type BulkFileOptions =
  | { mode: 'rename'; prefix?: string; suffix?: string }
  | { mode: 'organize'; targetDirectory: string }
  | { mode: 'replace'; find: string; replacement: string }

export function filterFilePaths(paths: string[], filter: string): string[] {
  const query = filter.trim().toLocaleLowerCase()
  return paths.filter(path => path.toLocaleLowerCase().includes(query))
}

export function assertPlanBounds(operations: FileOperation[]): void {
  if (operations.length > MAX_BULK_FILES) throw new Error('单批最多 50 项，请拆分计划')
  const paths = new Set<string>()
  let bytes = 0
  for (const operation of operations) {
    if (['file.rename', 'file.move', 'file.copy'].includes(operation.operation)) {
      buildFileOperation(operation.operation.slice(5) as 'rename' | 'move' | 'copy', String(operation.arguments.source || ''), String(operation.arguments.destination || ''), '', String(operation.arguments.expected_version_token || ''))
    }
    for (const value of [operation.arguments.path, operation.arguments.source, operation.arguments.destination]) {
      if (!value) continue
      const path = String(value).replaceAll('\\', '/').toLowerCase()
      if (paths.has(path)) throw new Error(`计划路径冲突：${value}。请移除冲突项或修改目标后重新预检`)
      paths.add(path)
    }
    if (typeof operation.arguments.content === 'string') bytes += new TextEncoder().encode(operation.arguments.content).length
  }
  if (bytes > MAX_BULK_TOTAL_BYTES) throw new Error('工作台文本计划总量超过 8 MiB 上限，请拆分计划')
}

export function buildBulkFilePlan(sources: BulkFileSource[], options: BulkFileOptions): FileOperation[] {
  if (!sources.length || sources.length > MAX_BULK_FILES) throw new Error('请选择 1 至 50 个文件')
  if (options.mode === 'replace' && !options.find) throw new Error('查找文本不能为空；只支持字面替换，不执行正则')
  if (options.mode === 'rename' && [options.prefix, options.suffix].some(part => part && (/[\\/:*?"<>|]/.test(part) || [...part].some(character => character.charCodeAt(0) < 32)))) throw new Error('名称前后缀不能包含路径分隔符或 Windows 保留字符')
  const encoder = new TextEncoder()
  let totalReadBytes = 0
  const operations: FileOperation[] = []
  for (const source of sources) {
    const path = source.path.replaceAll('\\', '/')
    const split = path.lastIndexOf('/')
    const directory = split < 0 ? '' : path.slice(0, split + 1)
    const name = path.slice(split + 1)
    const dot = name.lastIndexOf('.')
    const stem = dot > 0 ? name.slice(0, dot) : name
    const extension = dot > 0 ? name.slice(dot) : ''
    if (options.mode === 'replace') {
      if (source.truncated || typeof source.content !== 'string') throw new Error(`文件 ${path} 缺少完整文本或已截断，禁止覆盖`)
      const size = encoder.encode(source.content).length
      totalReadBytes += size
      if (size > MAX_BULK_TEXT_BYTES || totalReadBytes > MAX_BULK_TOTAL_BYTES) throw new Error('文本读取超过每文件 1 MiB / 总计 8 MiB 上限')
      if (!source.content.includes(options.find)) continue
      // split/join treats both strings literally, including "$&" and regex syntax.
      const segments = source.content.split(options.find)
      const outputLength = source.content.length + (segments.length - 1) * (options.replacement.length - options.find.length)
      if (outputLength > MAX_BULK_TEXT_BYTES) throw new Error(`替换结果 ${path} 超过 1 MiB 上限`)
      const content = segments.join(options.replacement)
      if (content === source.content) continue
      if (encoder.encode(content).length > MAX_BULK_TEXT_BYTES) throw new Error(`替换结果 ${path} 超过 1 MiB 上限`)
      operations.push(buildFileOperation('edit', path, '', content, source.version_token))
    } else {
      const destination = options.mode === 'rename'
        ? `${directory}${options.prefix || ''}${stem}${options.suffix || ''}${extension}`
        : `${options.targetDirectory.replaceAll('\\', '/').replace(/\/$/, '')}/${extension.slice(1).toLowerCase() || '无扩展名'}/${name}`
      operations.push(buildFileOperation(options.mode === 'rename' ? 'rename' : 'move', path, destination, '', source.version_token))
    }
  }
  assertPlanBounds(operations)
  return operations
}

export function fileStepLabel(value: string): string {
  const labels: Record<string, string> = { preview: '已预检，未执行', preflight: '正在预检', planned: '待执行', backup_verified: '备份已核验', effect_started: '已开始修改', effect_observed: '修改已记录', committed: '已完成', preflight_failed: '预检失败，未执行', failed: '失败', compensation_started: '正在恢复', compensated: '已恢复', rolled_back: '已回滚', rollback_failed: '回滚未完成', reconciled: '已核对现场', needs_attention: '需要人工核对', completed: '现场与完成证据一致', not_started: '未开始', partial: '部分完成', restored: '已恢复', ok: '成功', error: '失败', cancelled: '已取消' }
  return labels[value] || `未识别状态 (${value || 'unknown'})`
}
