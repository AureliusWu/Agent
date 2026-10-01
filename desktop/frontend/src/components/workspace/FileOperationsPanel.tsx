import { useEffect, useRef, useState } from 'react'
import { api } from '../../api'
import type { PermissionMode } from '../../types'
import { buildFileOperation, canConfirmFileOperation, deletionSummary, FILE_ACTION_LABELS, fileOperationError } from '../../shared/fileOperationPolicy'
import type { FileAction, FileOperation, FileToolResult } from '../../shared/fileOperationPolicy'
import { assertPlanBounds, buildBulkFilePlan, fileStepLabel, MAX_BULK_FILES, MAX_BULK_TEXT_BYTES, MAX_BULK_TOTAL_BYTES } from '../../shared/bulkFilePlan'
import type { BulkFileOptions, BulkFileSource } from '../../shared/bulkFilePlan'
import { LatestRequest } from '../../shared/latestRequest'
import '../../styles/files.css'

interface FileChange { change_id: string; operation: string; created_at: string; paths: string[]; status: string; retained_bytes: number | null; capacity_status: string }
interface RecoveryPage { items: FileChange[]; total: number | null; limit: number; offset: number; truncated: boolean }
interface TransactionStep { operation_id: string; step_index: number; operation: string; state: string; observed_state?: string; change_id?: string; before?: Record<string, unknown>; after?: Record<string, unknown>; result?: FileToolResult }
interface Transaction { operation_id: string; status: string; created_at?: string; plan?: FileOperation[]; steps?: TransactionStep[]; result?: FileToolResult; automatic_replay?: boolean }
interface Props { conversationId: number; workspace: string; mode: PermissionMode; selected: string; selectedFiles?: string[]; onChanged: () => void }

// FilesPanel keys this scope by conversation/workspace/permission. No plan
// bodies, versions or grants are persisted in browser storage.
export function FileOperationsPanel({ conversationId, workspace, mode, selected, selectedFiles = [], onChanged }: Props) {
  const [action, setAction] = useState<FileAction>('create')
  const [source, setSource] = useState('')
  const [destination, setDestination] = useState('')
  const [content, setContent] = useState('')
  const [version, setVersion] = useState('')
  const [diff, setDiff] = useState('')
  const [plan, setPlan] = useState<FileOperation[]>([])
  const [preview, setPreview] = useState<FileToolResult | null>(null)
  const [result, setResult] = useState<FileToolResult | null>(null)
  const [changes, setChanges] = useState<FileChange[]>([])
  const [recoveryPage, setRecoveryPage] = useState<RecoveryPage>({ items: [], total: null, limit: 25, offset: 0, truncated: false })
  const [transactions, setTransactions] = useState<Transaction[]>([])
  const [totalTransactions, setTotalTransactions] = useState(0)
  const [detail, setDetail] = useState<Transaction | null>(null)
  const [reconciliation, setReconciliation] = useState<Transaction | null>(null)
  const [bulkMode, setBulkMode] = useState<BulkFileOptions['mode']>('rename')
  const [prefix, setPrefix] = useState('')
  const [suffix, setSuffix] = useState('')
  const [targetDirectory, setTargetDirectory] = useState('整理后')
  const [find, setFind] = useState('')
  const [replacement, setReplacement] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const working = useRef(false)
  const planId = useRef('')
  const renderedPlanId = planId.current
  const reader = useRef(new LatestRequest())
  const historyReader = useRef(new LatestRequest())
  const readonly = mode === 'readonly'
  const destructive = deletionSummary(plan)
  const selectedKey = selectedFiles.join('\n')
  useEffect(() => { const gate = reader.current; const historyGate = historyReader.current; mounted.current = true; return () => { mounted.current = false; gate.begin(); historyGate.begin() } }, [])
  useEffect(() => {
    reader.current.begin()
    if (selected) { setSource(selected); setAction('edit'); setVersion(''); setContent(''); setDiff('') }
  }, [selected, selectedKey])

  const execute = (tool: string, args: Record<string, unknown>, approvalTokens: string[] = []) => {
    if (!mounted.current) throw new Error('工作区已切换，旧请求已停止')
    return api<FileToolResult>('/api/tools/execute', { method: 'POST', body: JSON.stringify({ conversation_id: conversationId, workspace, permission_mode: mode, tool, arguments: args, approval_tokens: approvalTokens }) })
  }
  const current = (request: number) => mounted.current && reader.current.isLatest(request)
  const detailUrl = (id: string) => `/api/file-transactions/${encodeURIComponent(id)}?conversation_id=${conversationId}`

  async function loadChanges(offset = 0) {
    const request = historyReader.current.begin()
    const [response, batches] = await Promise.all([
      api<RecoveryPage>(`/api/file-recovery?conversation_id=${conversationId}&limit=25&offset=${offset}`),
      api<{ items: Transaction[]; total: number }>(`/api/file-transactions?conversation_id=${conversationId}&limit=25&offset=0`),
    ])
    if (!mounted.current || !historyReader.current.isLatest(request)) return
    setChanges(response.items); setRecoveryPage(response)
    setTransactions(batches.items); setTotalTransactions(batches.total)
  }
  // Scope is remounted by FilesPanel; pending reads cannot enter a new scope.
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void loadChanges().catch(caught => { if (mounted.current) setError((caught as Error).message) }) }, [])

  async function run(work: () => Promise<void>) {
    if (working.current || !mounted.current) return
    working.current = true
    setBusy(true); setError('')
    try { await work() } catch (caught) { if (mounted.current) setError((caught as Error).message) }
    finally { working.current = false; if (mounted.current) setBusy(false) }
  }
  async function authorized(tool: string, args: Record<string, unknown>, confirmation: string) {
    if (readonly) throw new Error('只读模式禁止修改，也不能通过确认解除')
    let response = await execute(tool, args)
    if (!mounted.current) return null
    if (canConfirmFileOperation(mode, response)) {
      if (!window.confirm(confirmation)) { setResult({ status: 'cancelled', message: '已拒绝授权，未继续执行' }); return null }
      response = await execute(tool, args, [response.approval_key!])
    }
    if (!mounted.current) return null
    const visibleResult = { ...response }
    delete visibleResult.approval_key
    setResult(visibleResult)
    if (response.status !== 'ok' || response.success === false) throw new Error(fileOperationError(response))
    return response
  }
  function changePlan(next: FileOperation[]) {
    assertPlanBounds(next)
    planId.current = crypto.randomUUID()
    setPlan(next); setPreview(null); setResult(null)
  }
  function clearEditorVersion() { setVersion(''); setDiff(''); reader.current.begin() }
  async function readSelected() {
    const request = reader.current.begin()
    const response = await execute('read_file', { path: source, max_chars: 200_000, preserve_newlines: true })
    if (!current(request)) return
    if (response.status !== 'ok') throw new Error(fileOperationError(response))
    if (response.truncated) throw new Error('文件内容超过编辑上限，不能使用截断内容覆盖原文件')
    setContent(String(response.content ?? '')); setVersion(String(response.version_token ?? '')); setDiff('')
  }
  async function stage() {
    if (readonly) throw new Error('只读模式禁止修改')
    const request = reader.current.begin()
    let sourceVersion = version
    if (action !== 'create' && action !== 'edit') {
      const metadata = await execute('file_metadata', { path: source })
      if (!current(request)) return
      if (metadata.status !== 'ok') throw new Error(fileOperationError(metadata))
      if (metadata.type !== 'file') throw new Error('此操作区仅处理单个文件；目录请在文件列表中浏览')
      sourceVersion = String(metadata.version_token ?? '')
    }
    const operation = buildFileOperation(action, source, destination, content, sourceVersion)
    assertPlanBounds([...plan, operation])
    if (action === 'edit' || action === 'create') {
      const response = await execute('file_diff', { path: source, content })
      if (!current(request)) return
      if (response.status !== 'ok') throw new Error(fileOperationError(response))
      setDiff(String(response.diff || '没有文本差异'))
    }
    changePlan([...plan, operation])
  }
  async function stageBulk() {
    if (readonly) throw new Error('只读模式禁止修改')
    if (!selectedFiles.length || selectedFiles.length > MAX_BULK_FILES) throw new Error('请选择 1 至 50 个文件')
    if (bulkMode === 'replace' && !find) throw new Error('查找文本不能为空')
    const request = reader.current.begin()
    const sources: BulkFileSource[] = []
    let totalBytes = 0
    for (const path of selectedFiles) {
      const metadata = await execute('file_metadata', { path })
      if (!current(request)) return
      if (metadata.status !== 'ok') throw new Error(fileOperationError(metadata))
      if (metadata.type !== 'file') throw new Error(`${path} 不是普通文件`)
      if (bulkMode === 'replace') {
        const size = Number(metadata.size)
        if (metadata.encoding === 'binary' || metadata.text_read_within_limit === false) throw new Error(`${path} 未确认为可安全编辑的文本，已停止`)
        if (!Number.isFinite(size) || size < 0 || size > MAX_BULK_TEXT_BYTES) throw new Error(`${path} 未确认在 1 MiB 文本上限内`)
        totalBytes += size
        if (totalBytes > MAX_BULK_TOTAL_BYTES) throw new Error('文本读取总量超过 8 MiB 上限，请减少选中文件')
        const response = await execute('read_file', { path, max_chars: 200_000, preserve_newlines: true })
        if (!current(request)) return
        if (response.status !== 'ok') throw new Error(fileOperationError(response))
        sources.push({ path, version_token: String(response.version_token || ''), content: typeof response.content === 'string' ? response.content : undefined, truncated: Boolean(response.truncated) })
      } else sources.push({ path, version_token: String(metadata.version_token || '') })
    }
    const options: BulkFileOptions = bulkMode === 'rename' ? { mode: 'rename', prefix, suffix } : bulkMode === 'organize' ? { mode: 'organize', targetDirectory } : { mode: 'replace', find, replacement }
    const additions = buildBulkFilePlan(sources, options)
    if (!additions.length) throw new Error('没有需要修改的文件；未生成计划')
    assertPlanBounds([...plan, ...additions])
    const diffs: string[] = []
    for (const item of additions.filter(item => item.operation === 'file.write')) {
      const response = await execute('file_diff', { path: item.arguments.path, content: item.arguments.content })
      if (!current(request)) return
      if (response.status !== 'ok') throw new Error(fileOperationError(response))
      diffs.push(`--- ${item.arguments.path} ---\n${String(response.diff || '没有文本差异').slice(0, 40_000)}`)
    }
    setDiff(diffs.join('\n')); changePlan([...plan, ...additions])
  }
  async function runBatch(dryRun: boolean) {
    if (renderedPlanId !== planId.current) return
    if (!plan.length || (!dryRun && !preview)) return
    const summary = destructive || `本批次包含 ${plan.length} 项文件操作。备份和当前文件均通过核验后才可恢复。`
    if (!dryRun && !window.confirm(`执行已预览的 ${plan.length} 项操作？\n${summary}`)) return
    const args: Record<string, unknown> = { operations: plan, dry_run: dryRun, operation_id: renderedPlanId }
    if (!dryRun) args.expected_plan_hash = preview!.plan_hash
    setPreview(null)
    let response: FileToolResult | null
    try { response = await authorized('file_batch', args, `${dryRun ? '预检' : '执行'}当前批次？\n${summary}`) }
    catch (caught) {
      if (!dryRun && mounted.current) { await loadChanges(); if (mounted.current) onChanged() }
      throw caught
    }
    if (!response) return
    if (dryRun) {
      if (response.operation_id !== renderedPlanId || !/^[a-f\d]{64}$/.test(String(response.plan_hash || ''))) throw new Error('核心未返回有效的批次身份与计划摘要，不能执行')
      setPreview(response)
    } else {
      changePlan([]); setVersion(''); setContent(''); setDiff(''); setResult(response)
      await loadChanges(); if (mounted.current) onChanged()
    }
  }
  async function showDetail(id: string) {
    const response = await api<Transaction>(detailUrl(id))
    if (mounted.current) { setDetail(response); setReconciliation(null) }
    return response
  }
  async function reconcileBatch(id: string) {
    const response = await api<Transaction>(`/api/file-transactions/${encodeURIComponent(id)}/reconcile`, { method: 'POST', body: JSON.stringify({ conversation_id: conversationId }) })
    if (!mounted.current) return
    setReconciliation(response); await loadChanges()
  }
  async function restoreBatch(id: string) {
    if (readonly) throw new Error('只读模式禁止恢复文件')
    const batch = await showDetail(id)
    if (!mounted.current) return
    if (!batch.steps?.some(step => step.change_id && ['committed', 'effect_observed', 'needs_attention', 'failed'].includes(step.state))) throw new Error('此批次没有已确认的恢复候选证据；请先核对现场，不会自动续跑')
    if (!window.confirm(`请求恢复批次 ${id}？\n核心会对整批逆序预检；当前版本、备份或权限冲突将停止。不会强制覆盖，也不会自动重放原计划。`)) return
    const response = await authorized('undo_file_batch', { operation_id: id }, `允许恢复此批次 ${id}？`)
    if (response) { setPreview(null); clearEditorVersion(); await showDetail(id); await loadChanges(); if (mounted.current) onChanged() }
  }
  async function undo(change: FileChange) {
    const targets = change.paths.join('、') || change.change_id
    if (!window.confirm(`撤销 ${change.operation}？\n目标：${targets}\n恢复依赖原备份；当前文件有冲突时停止，不强制覆盖。`)) return
    const response = await authorized('undo_file_change', { change_id: change.change_id }, `授权撤销变更 ${change.change_id}？\n目标：${targets}`)
    if (response) { setPreview(null); clearEditorVersion(); await loadChanges(); if (mounted.current) onChanged() }
  }
  function updateDestination(index: number, destination: string) {
    // An incomplete target input must invalidate its previous approval too.
    const next = plan.map((item, i) => i === index ? { ...item, arguments: { ...item.arguments, destination } } : item)
    planId.current = crypto.randomUUID(); setPlan(next); setPreview(null); setResult(null)
  }
  const latestCandidate = transactions.find(item => ['committed', 'rollback_failed', 'needs_attention', 'reconciled'].includes(item.status))
  const stepObservations = new Map(reconciliation?.steps?.map(step => [step.step_index, step.observed_state]))
  const results = Array.isArray(result?.results) ? result.results as FileToolResult[] : []
  const recoveryNotice = result?.error_code === 'batch_preflight_failed' ? '预检未通过，未启动文件修改。'
    : result?.rolled_back === false ? '回滚未完成：请查看批次步骤并核对现场；原备份和恢复材料保留，不会自动重放。'
      : result?.rolled_back === true && results.length ? '已完成本批次的核心回滚；可查看步骤记录核对结果。' : ''
  const measuredBytes = changes.reduce((sum, change) => sum + (change.retained_bytes ?? 0), 0)
  const unknownCapacity = changes.filter(change => change.retained_bytes === null).length
  const nextRecoveryPage = recoveryPage.total === null ? recoveryPage.truncated && changes.length === recoveryPage.limit : recoveryPage.offset + changes.length < recoveryPage.total

  return <section className="file-operations" aria-label="文件操作">
    <header><h3>文件操作</h3><span>{readonly ? '只读 · 写入和撤销均被禁用' : '版本校验 · 不覆盖已有目标 · 证据核验后恢复'}</span></header>
    <section className="file-bulk-form" aria-label="批量文件计划">
      <h4>多选计划 · {selectedFiles.length} / 50 个文件</h4>
      <p>在上方勾选文件。生成计划不写入文件；刷新后正文需重新填写，服务器记录不会自动重放。</p>
      <div className="file-operation-form">
        <label>批量操作<select value={bulkMode} disabled={busy} onChange={event => setBulkMode(event.target.value as BulkFileOptions['mode'])}><option value="rename">批量重命名</option><option value="organize">按扩展名分类移动</option><option value="replace">文本字面替换</option></select></label>
        {bulkMode === 'rename' && <><label>名称前缀<input value={prefix} disabled={busy} onChange={event => setPrefix(event.target.value)}/></label><label>名称后缀（扩展名前）<input value={suffix} disabled={busy} onChange={event => setSuffix(event.target.value)}/></label></>}
        {bulkMode === 'organize' && <label>分类目标目录<input value={targetDirectory} disabled={busy} onChange={event => setTargetDirectory(event.target.value)}/></label>}
        {bulkMode === 'replace' && <><label>查找文本<input value={find} disabled={busy} onChange={event => setFind(event.target.value)}/></label><label>替换为<input value={replacement} disabled={busy} onChange={event => setReplacement(event.target.value)}/></label><p>仅完整文本，每文件 ≤ 1 MiB 且 ≤ 200,000 字符，总计 ≤ 8 MiB；不支持正则，未命中的文件不加入计划。</p></>}
      </div>
      <button className="secondary" disabled={busy || readonly || !selectedFiles.length} onClick={() => void run(stageBulk)}>生成多选计划与差异</button>
    </section>
    <h4>单文件操作</h4>
    <div className="file-operation-form">
      <label>操作<select value={action} disabled={busy} onChange={event => { setAction(event.target.value as FileAction); clearEditorVersion() }}>{Object.entries(FILE_ACTION_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label>文件路径<input value={source} disabled={busy} placeholder="例如 docs/note.md" onChange={event => { setSource(event.target.value); clearEditorVersion() }}/></label>
      {['rename', 'move', 'copy'].includes(action) && <label>目标路径<input value={destination} disabled={busy} placeholder="目标必须尚不存在" onChange={event => setDestination(event.target.value)}/></label>}
      {(action === 'create' || action === 'edit') && <label className="file-content-label">文本内容<textarea value={content} disabled={busy} onChange={event => setContent(event.target.value)} rows={6}/></label>}
    </div>
    <div className="panel-toolbar">{action === 'edit' && <button className="secondary" disabled={busy || !source} onClick={() => void run(readSelected)}>读取文件版本</button>}<button className="secondary" disabled={busy || readonly || !source} onClick={() => void run(stage)}>加入计划并预览差异</button></div>
    {diff && <pre className="file-diff" aria-label="文本差异">{diff}</pre>}
    {plan.length > 0 && <div className="file-plan"><h4>待执行计划 · {plan.length} 项</h4><ol>{plan.map((item, index) => <li key={index}><code>{FILE_ACTION_LABELS[item.operation.replace('file.', '') as FileAction] || (item.operation === 'file.write' ? '写入' : item.operation)}</code><span>{String(item.arguments.path || item.arguments.source)}{item.arguments.destination !== undefined && <label>目标（不覆盖）<input aria-label={`第 ${index + 1} 项目标路径`} value={String(item.arguments.destination)} disabled={busy} onChange={event => updateDestination(index, event.target.value)}/></label>}</span><button className="secondary" disabled={busy} onClick={() => { changePlan(plan.filter((_, i) => i !== index)); setDiff('') }}>移除</button></li>)}</ol>{destructive && <p className="file-delete-summary">{destructive}</p>}<p>{preview ? '核心预检通过；执行时仍会再次检查文件版本。' : '计划尚未执行，请先预检。冲突时可移除项或修改目标，再重新预检。'}</p><div className="panel-toolbar"><button className="secondary" disabled={busy || readonly} onClick={() => void run(async () => { assertPlanBounds(plan); await runBatch(true) })}>预检批次</button><button className="primary" disabled={busy || readonly || !preview} onClick={() => void run(() => runBatch(false))}>执行 {plan.length} 项</button></div></div>}
    {busy && <p role="status">正在处理，请稍候；切换后忽略旧结果，已发出的操作以核心记录为准。</p>}
    {error && <p className="panel-error" role="alert">{error}</p>}
    {result && Number.isInteger(result.failed_index) && <p role="status">失败步骤：第 {Number(result.failed_index) + 1} 项；后续步骤未继续。</p>}
    {recoveryNotice && <p role="status">{recoveryNotice}</p>}
    {result && <section className="file-operation-result" aria-label="操作结果"><h4>操作结果 · {fileStepLabel(String(result.status))}</h4><p>{String(result.message || result.error_message || '以核心返回结果和步骤记录为准。')}</p>{results.length > 0 && <ol>{results.map((item, i) => <li key={i}>第 {i + 1} 项 · {fileStepLabel(String(item.status))}{item.error_message ? ` · ${item.error_message}` : ''}</li>)}</ol>}<details><summary>高级诊断 JSON</summary><pre>{JSON.stringify(result, null, 2)}</pre></details></section>}
    <section className="file-transactions" aria-label="服务器批次记录"><h4>服务器批次记录 · {totalTransactions} 条</h4><p>仅保存路径、版本和步骤元数据，不保存待写入正文；核对只检查现场，不恢复、不继续执行。</p><div className="panel-toolbar"><button className="secondary" disabled={busy} onClick={() => void run(loadChanges)}>刷新批次记录</button><button className="secondary" disabled={busy || readonly || !latestCandidate} onClick={() => void run(() => restoreBatch(latestCandidate!.operation_id))}>恢复最近批次</button></div>{totalTransactions > transactions.length && <p>当前展示最近 {transactions.length} 条记录。</p>}<ol>{transactions.map(item => <li key={item.operation_id}><div><strong>{fileStepLabel(item.status)}</strong><time>{item.created_at}</time><code>{item.operation_id}</code></div><button className="secondary" disabled={busy} onClick={() => void run(async () => { await showDetail(item.operation_id) })}>查看步骤</button></li>)}</ol>
      {detail && <section className="file-transaction-detail" aria-label="批次详情"><h4>批次详情 · {fileStepLabel(detail.status)}</h4><code>{detail.operation_id}</code><div className="panel-toolbar"><button className="secondary" disabled={busy} onClick={() => void run(() => reconcileBatch(detail.operation_id))}>核对现场（不修改文件）</button><button className="secondary" disabled={busy || readonly || !detail.steps?.some(step => step.change_id)} onClick={() => void run(() => restoreBatch(detail.operation_id))}>请求恢复此批次</button></div>{reconciliation && <p role="status">核对结果：{fileStepLabel(reconciliation.status)}。未自动继续任何文件操作。</p>}<ol>{detail.steps?.map(step => <li key={step.operation_id}><div><strong>第 {step.step_index + 1} 项 · {step.operation}</strong><span>{fileStepLabel(step.state)}{stepObservations.get(step.step_index) && ` · ${fileStepLabel(stepObservations.get(step.step_index)!)}`}</span><span>{Object.keys(step.before || {}).join('、')}</span><span>{step.change_id ? '有备份记录；是否可恢复仍需核心核验' : '尚无可恢复备份证据'}</span></div></li>)}</ol>{!detail.steps?.length && <p>此记录只有预览或历史元数据，没有完整步骤证据。正文需重新填写，不会自动重放。</p>}<details><summary>高级诊断 JSON</summary><pre>{JSON.stringify(detail, null, 2)}</pre></details></section>}
    </section>
    <details className="file-undo-timeline"><summary>撤销时间线 · 本页 {changes.length} 条</summary><p>记录可用不等于一定可恢复；权限、版本及备份会在恢复时重新核验。不会自动删除备份。</p><p>本页已计量保留容量 {(measuredBytes / 1_048_576).toFixed(2)} MiB{unknownCapacity > 0 ? `，另有 ${unknownCapacity} 条容量未知` : ''}；不是整个工作区总量。</p><div className="panel-toolbar"><button className="secondary" disabled={busy} onClick={() => void run(() => loadChanges(recoveryPage.offset))}>刷新记录</button><button className="secondary" disabled={busy || recoveryPage.offset === 0} onClick={() => void run(() => loadChanges(Math.max(0, recoveryPage.offset - recoveryPage.limit)))}>上一页恢复记录</button><button className="secondary" disabled={busy || !nextRecoveryPage || recoveryPage.offset + recoveryPage.limit > 100_000} onClick={() => void run(() => loadChanges(recoveryPage.offset + recoveryPage.limit))}>下一页恢复记录</button></div>{recoveryPage.truncated && <p>记录扫描有上限；未展示的记录和未知容量不会被当成零。</p>}{changes.length === 0 && <p>当前页没有恢复记录。</p>}<ol>{changes.map(change => <li key={change.change_id}><div><strong>{change.operation} · {change.status === 'available' ? '记录可用 · 恢复能力未核验' : change.status === 'legacy' ? '历史记录 · 证据不足' : fileStepLabel(change.status)}</strong><time>{change.created_at}</time><span>{change.paths.join('、')}</span><span>{change.retained_bytes === null ? '保留容量未知' : `${(change.retained_bytes / 1_048_576).toFixed(2)} MiB`}</span></div><button className="secondary" disabled={busy || readonly || !['available', 'needs_attention'].includes(change.status)} onClick={() => void run(() => undo(change))}>撤销此项</button></li>)}</ol></details>
  </section>
}
