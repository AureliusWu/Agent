import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, History } from 'lucide-react'
import { api } from '../api'
import type { VerificationReport } from '../types'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

interface ToolRun {
  id: number
  tool: string
  status: string
  source: string
  confirmed: number
  duration_ms: number
  input: Record<string, unknown>
  output: Record<string, unknown>
}

interface ModelRun {
  provider: string
  model: string
  phase: string
  route_tier: string
  task_type: string
  route_confidence: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  estimated_cost_usd: number
  duration_ms: number
  success: number
}

interface PhaseCost {
  calls: number
  tokens: number
  duration_ms: number
  estimated_cost_usd: number
}

interface TaskTrace {
  id: string
  prompt: string
  status: string
  termination_reason?: string
  current_phase: string
  resume_count: number
  model_calls: number
  total_tokens: number
  input_tokens: number
  output_tokens: number
  estimated_cost_usd: number
  cache_hits: number
  cache_misses: number
  model_route: {tier?:string; model?:string; task_type?:string; reason?:string}
  phase_costs: Record<string, PhaseCost>
  model_runs: ModelRun[]
  created_at: string
  verification: VerificationReport | null
  plan: {steps:Array<{id:string; description:string}>; acceptance_criteria:Array<{id:string; description:string}>} | null
  verification_attempts: Array<{attempt:number; status:string}>
  repair_runs: Array<{attempt:number; status:string; retry_scope:string[]; reason?:string}>
  checkpoints: Array<{sequence:number; phase:string; reason:string; created_at:string}>
  operations: Array<{execution_id:string; checkpoint_sequence:number; tool:string; status:string; side_effect:number}>
  tool_runs: ToolRun[]
  skill_runs: Array<{name:string; path:string; content_chars:number}>
}

export function AuditPanel() {
  const [tasks, setTasks] = useState<TaskTrace[]>([])
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => { api<TaskTrace[]>('/api/tasks/recent').then(setTasks).catch(() => {}) }, [])

  return <section className="content-panel">
    <PanelHeader icon={<History/>} title="执行轨迹" subtitle="任务、工具、确认、验证与文件差异"/>
    <div className="task-traces">{tasks.map(task => {
      const expanded = open === task.id
      return <article key={task.id} className="task-trace">
        <button onClick={() => setOpen(expanded ? null : task.id)}>
          {expanded ? <ChevronDown/> : <ChevronRight/>}
          <span className={`audit-status ${task.status === 'completed' ? 'ok' : task.status === 'failed' ? 'error' : ''}`}/>
          <div><strong>{task.prompt}</strong><p>{task.termination_reason || task.status}</p></div>
          <time>{new Date(task.created_at).toLocaleString()}</time><code>{task.status}</code>
        </button>
        {expanded && <div className="trace-details">
          {task.checkpoints.length>0&&<div className="trace-recovery"><strong>Recovery</strong><span>#{task.checkpoints[0].sequence} · {task.checkpoints[0].phase} · {task.checkpoints[0].reason}</span><small>{task.checkpoints.length} 个检查点 · 恢复 {task.resume_count} 次</small></div>}
          {task.operations.filter(operation=>operation.status!=='completed').map(operation=><div className="trace-operation" key={operation.execution_id}><strong>{operation.tool}</strong><span>#{operation.checkpoint_sequence} · {operation.status}</span><small>{operation.side_effect?'副作用操作':'只读操作'}</small></div>)}
          {task.plan && <div className="trace-plan"><strong>Planner</strong><span>{task.plan.steps.map(step=>step.description).join(' → ')}</span><small>{task.plan.acceptance_criteria.length} 条验收条件</small></div>}
          {task.verification && <div className="trace-verification"><strong>{task.verification.summary}</strong><span>{task.verification.evaluation?`${task.verification.evaluation.score} 分 · `:''}{task.verification.checks.length} 项检查</span></div>}
          {task.repair_runs.map(repair=><div className="trace-repair" key={repair.attempt}><strong>Repair {repair.attempt}</strong><span>{repair.retry_scope.join('、')||'无返工范围'}</span><small>{repair.status}</small></div>)}
          <div className="trace-cost">
            <strong>模型成本</strong>
            <span>{task.model_calls} 次调用 · {task.input_tokens.toLocaleString()} 输入 / {task.output_tokens.toLocaleString()} 输出 Token</span>
            <small>{task.estimated_cost_usd>0?`$${task.estimated_cost_usd.toFixed(6)}`:'未配置模型单价'} · 缓存 {task.cache_hits}/{task.cache_hits+task.cache_misses}</small>
            {Object.keys(task.phase_costs).length>0&&<div className="phase-costs">{Object.entries(task.phase_costs).map(([phase,cost])=><span key={phase}><b>{phase}</b>{cost.calls} 次 · {cost.tokens.toLocaleString()} Token · {cost.duration_ms} ms</span>)}</div>}
          </div>
          {task.model_runs.length>0&&<div className="model-runs">{task.model_runs.map((run,index)=><div key={`${run.phase}-${index}`}><code>{run.route_tier}:{run.model}</code><span>{run.phase} · {run.total_tokens.toLocaleString()} Token · {run.duration_ms} ms</span></div>)}</div>}
          {task.skill_runs.length>0&&<p>已加载 Skill：{task.skill_runs.map(skill=>skill.name).join('、')}</p>}
          {task.tool_runs.length === 0 ? <p className="empty-trace">没有工具调用</p> : task.tool_runs.map(run => <div className="tool-trace" key={run.id}>
            <header><code>{run.source}:{run.tool}</code><span>{run.confirmed ? '已确认' : '自动'} · {run.duration_ms} ms · {run.status}</span></header>
            <p>{JSON.stringify(run.input)}</p>
            {typeof run.output.diff === 'string' && <pre>{run.output.diff}</pre>}
            {typeof run.output.error_message === 'string' && <p className="trace-error">{run.output.error_message}</p>}
          </div>)}
        </div>}
      </article>
    })}</div>
  </section>
}
