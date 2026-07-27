import { Activity, FilePlus2, Files, History, Info, Plug, Waves } from 'lucide-react'
import type { View } from '../../types'

interface Props {
  hasConversation: boolean
  onUpload: () => void
  onNavigate: (view: View) => void
  onClose: () => void
}

export function AttachmentMenu(props: Props) {
  const navigate = (view: View) => { props.onNavigate(view); props.onClose() }
  return <div className="composer-popover attachment-menu" role="menu" aria-label="添加与更多能力">
    <div className="attachment-actions">
      <button disabled={!props.hasConversation} onClick={() => { props.onUpload(); props.onClose() }}><FilePlus2 size={17} /><span><strong>添加文件</strong><small>{props.hasConversation ? '上传到当前项目根目录' : '请先创建任务'}</small></span></button>
      <button onClick={() => navigate('files')}><Files size={17} /><span><strong>项目文件</strong><small>浏览、上传与撤销文件改动</small></span></button>
      <button onClick={() => navigate('extensions')}><Plug size={17} /><span><strong>技能与插件</strong><small>管理 Skill、MCP 与扩展包</small></span></button>
      <button onClick={() => navigate('memory')}><Waves size={17} /><span><strong>记忆</strong><small>编写、导入与导出个人/项目记忆</small></span></button>
      <button onClick={() => navigate('usage')}><Activity size={17} /><span><strong>用量统计</strong><small>Token、请求、模型与成本趋势</small></span></button>
      <button onClick={() => navigate('audit')}><History size={17} /><span><strong>执行记录</strong><small>任务轨迹、工具调用与文件差异</small></span></button>
      <button onClick={() => navigate('settings')}><Info size={17} /><span><strong>设置与关于</strong><small>连接、版本和诊断信息</small></span></button>
    </div>
    <div className="agent-config"><span>基础Agent · 自动工具调度</span></div>
  </div>
}
