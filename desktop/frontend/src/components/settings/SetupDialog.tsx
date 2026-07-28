import { FolderOpen, Plus, X } from 'lucide-react'
import { MODE_LABEL } from '../../constants'
import type { AgentProfile, PermissionMode } from '../../types'
import '../../Setup.css'

interface Props {
  workspace: string
  mode: PermissionMode
  profiles: AgentProfile[]
  agentProfileId: string
  onWorkspace: (value: string) => void
  onMode: (mode: PermissionMode) => void
  onProfile: (value: string) => void
  onClose: () => void
  onCreate: () => void
}

export function SetupDialog({
  workspace, mode, profiles, agentProfileId,
  onWorkspace, onMode, onProfile, onClose, onCreate,
}: Props) {
  const selectProfile = (profileId: string) => {
    const profile = profiles.find(item => item.id === profileId)
    onProfile(profileId)
    onMode(profile?.source === 'builtin' ? profile.default_permission : 'ask')
  }
  const chooseWorkspace = async () => {
    const { open } = await import('@tauri-apps/plugin-dialog')
    const selected = await open({ directory: true, multiple: false, title: '选择司忆可以访问的工作区' })
    if (typeof selected === 'string') onWorkspace(selected)
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title">
        <div className="dialog-title">
          <div>
            <h2 id="setup-title">创建 Agent 对话</h2>
            <p>明确选择本次允许访问的工作区</p>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>

        <label>
          工作区绝对路径
          <div className="workspace-picker">
            <input value={workspace} onChange={e => onWorkspace(e.target.value)} placeholder="请选择一个工作区" readOnly />
            <button type="button" className="secondary" onClick={chooseWorkspace}><FolderOpen size={15} />选择文件夹</button>
          </div>
          <small>司忆只能读取和修改这个目录中的文件。</small>
        </label>

        <label>
          专业 Agent
          <select value={agentProfileId} onChange={e => selectProfile(e.target.value)}>
            {profiles.map(item => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
        </label>

        <fieldset>
          <legend>权限模式</legend>
          <div className="setup-modes">
            {(['ask', 'agent', 'full'] as PermissionMode[]).map(item => (
              <button type="button" key={item} className={mode === item ? 'active' : ''} onClick={() => onMode(item)}>
                {MODE_LABEL[item]}
              </button>
            ))}
          </div>
        </fieldset>

        <div className="dialog-actions">
          <button className="secondary" onClick={onClose}>取消</button>
          <button className="primary" onClick={onCreate} disabled={!workspace.trim()}>
            <Plus size={16} />创建对话
          </button>
        </div>
      </div>
    </div>
  )
}
