import { FilePlus2, Files, History, Info, Plug, Waves } from 'lucide-react'
import { ORCHESTRATION_LABEL } from '../constants'
import type { AgentProfile, OrchestrationMode, View } from '../types'

interface Props {
  busy: boolean
  hasConversation: boolean
  profiles: AgentProfile[]
  agentProfileId: string
  orchestrationMode: OrchestrationMode
  onUpload: () => void
  onNavigate: (view: View) => void
  onProfile: (value: string) => void
  onOrchestration: (value: OrchestrationMode) => void
  onClose: () => void
}

export function AttachmentMenu(props: Props) {
  const navigate = (view: View) => { props.onNavigate(view); props.onClose() }
  return <div className="composer-popover attachment-menu" role="menu" aria-label="添加与更多能力">
    <div className="attachment-actions">
      <button disabled={!props.hasConversation} onClick={() => { props.onUpload(); props.onClose() }}><FilePlus2 size={17} /><span><strong>添加文件</strong><small>{props.hasConversation ? '上传到当前项目根目录' : '请先创建任务'}</small></span></button>
      <button onClick={() => navigate('files')}><Files size={17} /><span><strong>项目文件</strong><small>浏览、上传与撤销文件改动</small></span></button>
      <button onClick={() => navigate('extensions')}><Plug size={17} /><span><strong>技能与插件</strong><small>管理 Skill、MCP 与扩展包</small></span></button>
      <button onClick={() => navigate('memory')}><Waves size={17} /><span><strong>工程记忆</strong><small>查看与维护项目记忆</small></span></button>
      <button onClick={() => navigate('audit')}><History size={17} /><span><strong>执行与验证</strong><small>任务轨迹、审计与验证结果</small></span></button>
      <button onClick={() => navigate('settings')}><Info size={17} /><span><strong>设置与关于</strong><small>连接、版本和诊断信息</small></span></button>
    </div>
    <div className="agent-config">
      <label>Agent Profile
        <select disabled={props.busy} value={props.agentProfileId} onChange={event => props.onProfile(event.target.value)}>
          {!props.profiles.some(item => item.id === props.agentProfileId) && <option value={props.agentProfileId}>{props.agentProfileId}（已停用）</option>}
          {props.profiles.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}
        </select>
      </label>
      <label>协作方式
        <select disabled={props.busy} value={props.orchestrationMode} onChange={event => props.onOrchestration(event.target.value as OrchestrationMode)}>
          {(Object.keys(ORCHESTRATION_LABEL) as OrchestrationMode[]).map(value => <option value={value} key={value}>{ORCHESTRATION_LABEL[value]}</option>)}
        </select>
      </label>
    </div>
  </div>
}
