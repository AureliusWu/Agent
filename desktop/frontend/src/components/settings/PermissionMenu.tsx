import { Check, Shield } from 'lucide-react'
import { MODE_DESCRIPTION, MODE_LABEL } from '../../constants'
import type { PermissionMode } from '../../types'

export function PermissionMenu({ mode, onSelect }: { mode: PermissionMode; onSelect: (mode: PermissionMode) => void | Promise<void> }) {
  return <div className="composer-popover permission-menu" role="menu" aria-label="访问权限">
    <header><Shield size={16} /><strong>访问权限</strong></header>
    {(['readonly', 'ask', 'agent', 'full'] as PermissionMode[]).map(item => <button role="menuitemradio" aria-checked={mode === item} key={item} onClick={() => onSelect(item)}>
      <span><strong>{MODE_LABEL[item]}</strong><small>{MODE_DESCRIPTION[item]}</small></span>
      {mode === item && <Check size={16} />}
    </button>)}
  </div>
}
