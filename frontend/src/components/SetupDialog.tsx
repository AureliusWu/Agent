import { Plus, X } from 'lucide-react'
import { MODE_LABEL } from '../constants'
import type { PermissionMode } from '../types'
import '../Setup.css'

export function SetupDialog({workspace,mode,onWorkspace,onMode,onClose,onCreate}:{workspace:string;mode:PermissionMode;onWorkspace:(value:string)=>void;onMode:(mode:PermissionMode)=>void;onClose:()=>void;onCreate:()=>void}) {
  return <div className="modal-backdrop" role="presentation"><div className="setup-dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title"><div className="dialog-title"><div><h2 id="setup-title">创建 Agent 对话</h2><p>明确选择本次允许访问的工作区</p></div><button className="icon-btn" onClick={onClose} aria-label="关闭"><X size={18}/></button></div><label>工作区绝对路径<input value={workspace} onChange={event=>onWorkspace(event.target.value)} placeholder="<workspace-root>"/></label><fieldset><legend>权限模式</legend><div className="setup-modes">{(['ask','agent','full'] as PermissionMode[]).map(item=><button type="button" key={item} className={mode===item?'active':''} onClick={()=>onMode(item)}>{MODE_LABEL[item]}</button>)}</div></fieldset><div className="dialog-actions"><button className="secondary" onClick={onClose}>取消</button><button className="primary" onClick={onCreate}><Plus size={16}/>创建对话</button></div></div></div>
}
