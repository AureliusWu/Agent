import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, GitBranch, History, ShieldCheck } from 'lucide-react'
import { api } from '../../api'
import type { VerificationReport } from '../../types'
import { PanelHeader } from '../shared/PanelHeader'
import { costEstimateDetail, costEstimateLabel } from '../../shared/costPolicy'
import type { CostEstimate } from '../../shared/costPolicy'
import '../../styles/panels.css'

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

interface ModelRun extends CostEstimate {
  provider: string
  model: string
  phase: string
  route_tier: string
  task_type: string
  route_confidence: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  duration_ms: number
  success: number
}

interface PhaseCost extends CostEstimate {
  calls: number
  tokens: number
  duration_ms: number
}

interface AgentRun {
  id: string
  parent_agent_id: string | null
  role: string
  orchestration_mode: string
  status: string
  token_budget: number
  tokens_used: number
  tool_allowlist: string[]
  file_scope: string[]
  risk_level: string
  depth: number
  error?: string
}

interface TaskTrace extends CostEstimate {
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
  skill_runs: Array<{
    name:string
    path:string
    version:string
    source:string
    content_chars:number
    content_tokens:number
    trigger_reason:string
    dependency_chain:string[]
    status:string
    error?:string|null
  }>
  data_flows: Array<{source:string; sink:string; classification:string; fields:string[]; redactions:number; allowed:boolean; reason:string}>
  security_snapshots: Array<{id:string; reason:string; status:string; file_count:number; total_bytes:number; restored_at?:string}>
  orchestration_mode: string
  child_agent_count: number
  agent_runs: AgentRun[]
  file_locks: Array<{path:string; holder_agent_id:string; status:string; version_before:string; version_after?:string}>
  task_intelligence: {
    requirements:Array<{description:string; requirement_type:string; required:number}>
    acceptance_conditions:Array<{description:string; verifier:string; evidence_required:number}>
    dependencies:Array<{node_id:string; depends_on:string; write_scope:string; estimated_tokens:number; estimated_seconds:number}>
    budget:{total_tokens:number; total_seconds:number; model_calls:number; tool_calls:number}|null
  }
  professional_trace:{roles:Array<{role:string; status:string; capabilities:string[]; attempt:number}>; messages:Array<{id:string; sender_role:string; recipient_role:string; message_type:string}>}
  performance:{traces:Array<{span_name:string; component:string; duration_ms:number; status:string}>; aggregates:Record<string,{samples:number; average_ms:number; max_ms:number}>}
  provider_policy:{preferred_provider:string; preferred_model:string; allow_paid_fallback:boolean; fallback_order:string[]; authorization_source?:string}|null
}

export function AuditPanel() {
  const [tasks, setTasks] = useState<TaskTrace[]>([])
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => { api<TaskTrace[]>('/api/tasks/recent').then(setTasks).catch(() => {}) }, [])

  return <section className="content-panel">
    <PanelHeader icon={<History/>} title="执行轨迹" subtitle="任务、工具、确认与文件差异"/>
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
          {task.task_intelligence.dependencies.length>0&&<div className="trace-plan"><strong>任务依赖图</strong><span>{task.task_intelligence.dependencies.map(node=>`${node.node_id}${node.depends_on!=='[]'?` ← ${JSON.parse(node.depends_on).join(', ')}`:''}`).join(' · ')}</span><small>{task.task_intelligence.requirements.length} 项需求 · {task.task_intelligence.acceptance_conditions.length} 项证据门禁{task.task_intelligence.budget?` · ${task.task_intelligence.budget.total_tokens.toLocaleString()} Token`:''}</small></div>}
          {task.professional_trace.roles.length>0&&<div className="trace-agents"><strong><GitBranch/>专业角色协作</strong><span>{task.professional_trace.roles.map(role=>`${role.role}:${role.status}`).join(' · ')}</span><small>{task.professional_trace.messages.length} 条结构化角色消息</small></div>}
          {task.repair_runs.map(repair=><div className="trace-repair" key={repair.attempt}><strong>Repair {repair.attempt}</strong><span>{repair.retry_scope.join('、')||'无返工范围'}</span><small>{repair.status}</small></div>)}
          {task.agent_runs.length>0&&<div className="trace-agents">
            <strong><GitBranch/>受控多 Agent</strong>
            <span>{task.orchestration_mode} · {task.agent_runs.filter(agent=>agent.depth>0).length} 个子 Agent · {task.file_locks.length} 次文件锁</span>
            <div>{task.agent_runs.map(agent=><p key={agent.id}><code>{agent.depth===0?'root':`child:${agent.role}`}</code><span>{agent.status} · {agent.tokens_used.toLocaleString()}/{agent.token_budget.toLocaleString()} Token · {agent.file_scope.join('、')}</span></p>)}</div>
          </div>}
          <div className="trace-cost">
            <strong>模型成本</strong>
            <span>{task.model_calls} 次调用 · {task.input_tokens.toLocaleString()} 输入 / {task.output_tokens.toLocaleString()} 输出 Token</span>
            <small>{costEstimateLabel(task, 6)} · {costEstimateDetail(task, 6)} · 缓存 {task.cache_hits}/{task.cache_hits+task.cache_misses}</small>
            {Object.keys(task.phase_costs).length>0&&<div className="phase-costs">{Object.entries(task.phase_costs).map(([phase,cost])=><span key={phase}><b>{phase}</b>{cost.calls} 次 · {cost.tokens.toLocaleString()} Token · {cost.duration_ms} ms</span>)}</div>}
          </div>
          {Object.keys(task.performance.aggregates).length>0&&<div className="trace-cost"><strong>性能轨迹</strong><span>{Object.entries(task.performance.aggregates).map(([name,value])=>`${name} ${Math.round(value.average_ms)} ms`).join(' · ')}</span><small>{task.performance.traces.length} 条真实运行样本</small></div>}
          {task.provider_policy&&<div className="trace-security"><strong><ShieldCheck/>Provider 策略</strong><span>{task.provider_policy.preferred_provider}:{task.provider_policy.preferred_model}</span><small>{task.provider_policy.allow_paid_fallback?'已显式授权付费回退':'禁止静默付费回退'}</small></div>}
          {task.task_intelligence.acceptance_conditions.length>0&&<div className="trace-security"><strong><ShieldCheck/>证据验收</strong><span>{task.task_intelligence.acceptance_conditions.map(item=>item.description).join(' · ')}</span><small>PASS 只能由实际证据产生</small></div>}
          {task.model_runs.length>0&&<div className="model-runs">{task.model_runs.map((run,index)=><div key={`${run.phase}-${index}`}><code>{run.route_tier}:{run.model}</code><span>{run.phase} · {run.total_tokens.toLocaleString()} Token · {run.duration_ms} ms</span></div>)}</div>}
          {task.skill_runs.length>0&&<div className="model-runs">
            {task.skill_runs.map((skill,index)=><div key={`${skill.path}-${index}`}>
              <code>{skill.name}@{skill.version}</code>
              <span>{skill.status} · {skill.source} · {skill.content_tokens.toLocaleString()} Token{skill.dependency_chain.length ? ` · 依赖 ${skill.dependency_chain.join(' → ')}` : ''}</span>
              {skill.error&&<small className="trace-error">{skill.error}</small>}
            </div>)}
          </div>}
          {(task.data_flows.length>0||task.security_snapshots.length>0)&&<div className="trace-security">
            <strong><ShieldCheck/>安全边界</strong>
            <span>{task.data_flows.length} 条数据流 · {task.data_flows.filter(flow=>!flow.allowed).length} 次阻止 · {task.data_flows.reduce((total,flow)=>total+flow.redactions,0)} 处去敏</span>
            <small>{task.security_snapshots.length>0?`${task.security_snapshots.length} 个可回滚快照 · 最新 ${task.security_snapshots[0].id.slice(0,8)}`:'本任务未产生高风险快照'}</small>
          </div>}
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
