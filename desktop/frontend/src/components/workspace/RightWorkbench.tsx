import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Activity, ArrowUp, ChevronRight, FileText, Folder, FolderOpen, Home, PanelRightClose, RefreshCw, Waves, X } from 'lucide-react'
import type { ContextStats, Conversation, PermissionMode, RuntimeEvent, VerificationReport, View } from '../../types'
import { useWorkspacePreview } from '../../hooks/useWorkspacePreview'
import { previewTabId, workbenchElementId } from '../../shared/rightWorkbench'
import { KokoroPanel } from '../kokoro/KokoroPanel'
import '../../styles/right-workbench.css'

interface Props {
  open: boolean
  active: Conversation | null
  workspace: string
  mode: PermissionMode
  context: ContextStats | null
  busy: boolean
  pendingCount: number
  recoverable: boolean
  messageCount: number
  runtimeEvents: RuntimeEvent[]
  verification: VerificationReport | null
  onClose: () => void
  onNavigate: (view: View) => void
  onCompact: () => void
  onChooseWorkspace: () => void
}

const fixedTabs = [
  { id: 'start', title: '开始', icon: Home },
  { id: 'files', title: '文件', icon: FolderOpen },
  { id: 'session', title: '会话', icon: Activity },
  { id: 'summary', title: '夏目心', icon: Waves },
]
const permissionLabels: Record<PermissionMode, string> = { readonly: '只读', ask: '请求批准', agent: 'Agent 权限', full: '完全访问' }
const verificationLabels = { passed: '已通过', failed: '未通过', partially_passed: '部分通过', blocked: '受阻' }

export function RightWorkbench(props: Props) {
  return <ScopedRightWorkbench key={`${props.active?.id ?? 'none'}:${props.workspace}:${props.mode}`} {...props} />
}

function ScopedRightWorkbench(props: Props) {
  const [selected, setSelected] = useState('start')
  const [filter, setFilter] = useState('')
  const tabsRef = useRef<HTMLDivElement>(null)
  const preview = useWorkspacePreview(props.active, props.workspace, props.mode, props.open && selected === 'files')
  const file = preview.files.find(item => previewTabId(item.path) === selected)
  const tabs = [...fixedTabs, ...preview.files.map(item => ({ id: previewTabId(item.path), title: item.path.split('/').pop()!, icon: FileText }))]
  const current = tabs.find(tab => tab.id === selected) || fixedTabs[0]
  const status = props.busy ? '正在执行' : props.pendingCount ? '等待批准' : props.recoverable ? '任务可继续' : props.active ? '待命' : '未开始'

  useEffect(() => { if (props.open) tabsRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' }) }, [selected, props.open])

  function closeFile(path: string) {
    let next = selected
    if (selected === previewTabId(path)) {
      const remaining = preview.files.filter(item => item.path !== path)
      next = remaining.length ? previewTabId(remaining.at(-1)!.path) : 'files'
      setSelected(next)
    }
    preview.closeFile(path)
    const target = workbenchElementId(next)
    const buttons = tabsRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    if (buttons) [...buttons].find(button => button.id === target)?.focus()
  }

  function closePanel() {
    props.onClose()
    if (typeof document !== 'undefined') document.querySelector<HTMLButtonElement>('[aria-controls="siyi-right-workbench"]')?.focus()
  }

  function tabKeys(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Delete' && file) { event.preventDefault(); closeFile(file.path); return }
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    const elements = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
    const index = tabs.findIndex(tab => tab.id === current.id)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowLeft' ? -1 : 1) + tabs.length) % tabs.length
    event.preventDefault(); setSelected(tabs[next].id); elements[next]?.focus()
  }

  return <aside id="siyi-right-workbench" className={`right-workbench${props.open ? ' open' : ''}`} aria-label="右侧工作台" hidden={!props.open} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); closePanel() } }}>
    <header className="right-workbench-header"><span><current.icon size={15} />{current.title}</span><button type="button" className="line-icon-button" onClick={closePanel} aria-label="收起右侧工作台" title="收起右侧工作台"><PanelRightClose size={17} /></button></header>
    <div ref={tabsRef} className="right-workbench-tabs" role="tablist" aria-label="工作台标签页" onKeyDown={tabKeys}>
      {tabs.map(tab => <div className="right-workbench-tab" key={tab.id} role="presentation"><button type="button" id={workbenchElementId(tab.id)} role="tab" aria-selected={current.id === tab.id} aria-controls={`${workbenchElementId(tab.id)}-content`} tabIndex={current.id === tab.id ? 0 : -1} onClick={() => setSelected(tab.id)} title={tab.id.startsWith('file:') ? tab.id.slice(5) : tab.title}><tab.icon size={14} /><span>{tab.title}</span></button>{tab.id.startsWith('file:') && <button type="button" className="right-workbench-tab-close" aria-label={`关闭 ${tab.id.slice(5)} 标签页`} title="关闭标签页（Delete）" onClick={() => closeFile(tab.id.slice(5))}><X size={12} /></button>}</div>)}
    </div>
    <section className="right-workbench-content" role="tabpanel" id={`${workbenchElementId(current.id)}-content`} aria-labelledby={workbenchElementId(current.id)} tabIndex={0}>
      {selected === 'start' && <div className="workbench-start">
        <div className="workbench-start-heading"><Home size={29} strokeWidth={1.4} /><h2>工作台</h2><p>文件、会话与摘要，在这里并行查看。</p></div>
        <button type="button" className="workbench-start-action" onClick={() => setSelected('files')}><FolderOpen size={21} /><span><strong>工作区文件</strong><small>浏览目录，打开只读文本标签页</small></span><ChevronRight size={15} /></button>
        <button type="button" className="workbench-start-action" onClick={() => setSelected('session')}><Activity size={21} /><span><strong>当前会话</strong><small>查看任务状态与待批准工具数量</small></span><ChevronRight size={15} /></button>
        <button type="button" className="workbench-start-action" onClick={() => setSelected('summary')}><Waves size={21} /><span><strong>夏目心与摘要</strong><small>保留角色、上下文摘要与记忆入口</small></span><ChevronRight size={15} /></button>
        <p className="workbench-note">此处文件预览只读，不会修改工作区文件。</p>
      </div>}
      {selected === 'files' && <div className="workbench-files">
        <div className="workbench-section-heading"><h2>工作区文件</h2><button type="button" className="line-icon-button" aria-label="刷新工作区文件" title="刷新" disabled={!preview.canBrowse || preview.loading} onClick={() => { void preview.loadDirectory() }}><RefreshCw size={15} /></button></div>
        {!preview.canBrowse ? <div className="workbench-empty"><FolderOpen size={28} /><p>{props.workspace ? '请先创建或选择此工作区的会话。' : '先选择工作区，再浏览文件。'}</p><button type="button" className="secondary" onClick={props.onChooseWorkspace}>选择工作区</button>{props.workspace && <button type="button" className="secondary" onClick={() => props.onNavigate('projects')}>打开项目与会话</button>}</div> : <>
          <p className="workbench-workspace-path" title={props.workspace}>{props.workspace}</p>
          <div className="workbench-directory"><button type="button" className="line-icon-button" aria-label="上一级目录" title="上一级目录" disabled={preview.directory === '.' || preview.loading} onClick={() => { setFilter(''); void preview.loadDirectory(preview.directory.split('/').slice(0, -1).join('/') || '.') }}><ArrowUp size={15} /></button><code title={preview.directory}>{preview.directory === '.' ? '工作区根目录' : preview.directory}</code></div>
          <label className="workbench-filter">筛选当前目录<input value={filter} placeholder="文件名或扩展名" onChange={event => setFilter(event.target.value)} /></label>
          {preview.loading && <p className="workbench-note" role="status">正在读取目录…</p>}
          <div className="workbench-file-list">{preview.entries.filter(item => item.name.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map(item => <button type="button" key={item.path} title={item.path} onClick={() => { if (item.type === 'directory') { setFilter(''); void preview.loadDirectory(item.path) } else if (preview.openFile(item)) setSelected(previewTabId(item.path)) }}>{item.type === 'directory' ? <Folder size={17} /> : <FileText size={17} />}<span>{item.name}</span>{item.type === 'directory' ? <ChevronRight size={14} /> : <small>{item.size !== undefined ? `${Math.ceil(item.size / 1024)} KB` : ''}</small>}</button>)}</div>
          {!preview.loading && !preview.entries.length && !preview.error && <p className="workbench-note">目录为空。</p>}
          <p className="workbench-note">最多显示 500 项；文本预览上限 2 MB / 40,000 字符。文件编辑与撤销仍在<button type="button" className="workbench-text-link" onClick={() => props.onNavigate('files')}>文件工作区</button>。</p>
        </>}
        {preview.error && <p className="panel-error" role="alert">{preview.error}</p>}
      </div>}
      {selected === 'session' && <div className="workbench-session">
        <h2>当前会话</h2>
        {props.active ? <><h3>{props.active.title || '未命名会话'}</h3><dl><dt>会话编号</dt><dd>#{props.active.id}</dd><dt>任务状态</dt><dd role="status">{status}</dd><dt>权限</dt><dd>{permissionLabels[props.mode]}</dd><dt>消息</dt><dd>{props.messageCount} 条</dd><dt>待批准工具</dt><dd>{props.pendingCount} 项</dd><dt>上下文估算</dt><dd>{props.context?.estimated_tokens?.toLocaleString() ?? '—'} Tokens</dd><dt>验证状态</dt><dd>{props.verification ? verificationLabels[props.verification.status] : '尚无报告'}</dd><dt>工具执行</dt><dd>{props.verification ? `${props.verification.tool_run_count} 次` : '—'}</dd><dt>运行事件</dt><dd>{props.runtimeEvents.length} 条</dd></dl><p className="workbench-workspace-path" title={props.workspace}>{props.workspace || '未绑定工作区'}</p><button type="button" className="secondary" onClick={() => props.onNavigate('audit')}>查看任务与操作记录</button><p className="workbench-note">这里只展示状态与数量，不展示凭据、工具参数或原始日志。</p></> : <p className="workbench-note">尚未选择会话。新建会话后可查看任务状态。</p>}
      </div>}
      {selected === 'summary' && <KokoroPanel open hasConversation={Boolean(props.active)} context={props.context} busy={props.busy} pendingCount={props.pendingCount} recoverable={props.recoverable} verification={props.verification} onClose={closePanel} onNavigate={props.onNavigate} onCompact={props.onCompact} />}
      {file && <div className="workbench-preview"><div className="workbench-preview-toolbar"><span>只读预览{file.encoding && ` · ${file.encoding}`}</span><button type="button" className="line-icon-button" aria-label={`重新读取 ${file.path}`} title="重新读取文件" disabled={file.loading} onClick={() => { void preview.reloadFile(file.path) }}><RefreshCw size={15} /></button></div><p className="workbench-file-path" title={file.path}>{file.path}</p>{file.loading ? <p className="workbench-note" role="status">正在读取文本…</p> : file.error ? <div><p className="panel-error" role="alert">{file.error}</p><p className="workbench-note">未修改文件，未自动放宽权限或重新授权。</p></div> : <>{file.truncated && <p className="workbench-preview-warning" role="status">仅显示前 40,000 字符，内容已截断。</p>}<pre aria-label={`${file.path} 只读内容`}>{file.content || '（空文件）'}</pre></>}</div>}
    </section>
  </aside>
}
