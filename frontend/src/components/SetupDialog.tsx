import { useState } from 'react'
import { Plus, X } from 'lucide-react'
import { MODE_LABEL } from '../constants'
import { isDesktop } from '../secrets'
import { hasWebAccessToken, setWebAccessToken } from '../api'
import type { AgentProfile, PermissionMode } from '../types'
import '../Setup.css'

interface Props {
  workspace: string
  mode: PermissionMode
  profiles: AgentProfile[]
  agentProfileId: string
  webAccessToken: string
  onWorkspace: (value: string) => void
  onMode: (mode: PermissionMode) => void
  onProfile: (value: string) => void
  onWebAccessToken: (value: string) => void
  onClose: () => void
  onCreate: () => void
}

export function SetupDialog({
  workspace, mode, profiles, agentProfileId,
  webAccessToken, onWorkspace, onMode, onProfile, onWebAccessToken,
  onClose, onCreate,
}: Props) {
  const selectProfile = (profileId: string) => {
    const profile = profiles.find(item => item.id === profileId)
    onProfile(profileId)
    onMode(profile?.source === 'builtin' ? profile.default_permission : 'ask')
  }
  const [savedHint, setSavedHint] = useState(hasWebAccessToken())

  const handleTokenUse = () => {
    setWebAccessToken(webAccessToken)
    setSavedHint(true)
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
          <input value={workspace} onChange={e => onWorkspace(e.target.value)} placeholder="D:\项目\workspace" />
        </label>

        <label>
          专业 Agent
          <select value={agentProfileId} onChange={e => selectProfile(e.target.value)}>
            {profiles.map(item => (
              <option key={item.id} value={item.id}>{item.name}</option>
            ))}
          </select>
        </label>

        {!isDesktop() && (
          <label>
            后端访问令牌
            <div style={{ display: 'flex', gap: 6 }}>
              <input
                type="password"
                value={webAccessToken}
                onChange={e => { onWebAccessToken(e.target.value); setSavedHint(false) }}
                placeholder="由后端管理员提供"
                style={{ flex: 1 }}
              />
              <button type="button" className="secondary" onClick={handleTokenUse} style={{ whiteSpace: 'nowrap' }}>
                本次使用
              </button>
            </div>
            {savedHint && <small style={{ color: 'var(--agent-accent)' }}>仅保存在当前页面内存，刷新后清除</small>}
            {!savedHint && !webAccessToken && <small>仅远程后端启用认证时需要；模型密钥由后端保管</small>}
          </label>
        )}

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
          <button className="primary" onClick={onCreate}>
            <Plus size={16} />创建对话
          </button>
        </div>
      </div>
    </div>
  )
}
