import { useEffect, useRef, useState } from 'react'
import { ChevronRight, File, Files, Folder, RotateCcw, Settings2, Upload } from 'lucide-react'
import { API_BASE, api } from '../api'
import type { Conversation, FileItem, PermissionMode } from '../types'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

export function FilesPanel({active, workspace, mode}: {active: Conversation|null; workspace:string; mode:PermissionMode}) {
  const [items,setItems]=useState<FileItem[]>([])
  const [path,setPath]=useState('.')
  const [error,setError]=useState('')
  const fileRef=useRef<HTMLInputElement>(null)
  const activeId = active?.id
  const load=async(next=path)=>{try{const result=await api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:active?.id,workspace,permission_mode:mode,tool:'list_files',arguments:{path:next}})});setItems(result.items);setPath(next);setError('')}catch(caught){setError((caught as Error).message)}}
  useEffect(()=>{if(!activeId)return;api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:activeId,workspace,permission_mode:mode,tool:'list_files',arguments:{path:'.'}})}).then(result=>{setItems(result.items);setPath('.');setError('')}).catch(caught=>setError(caught.message))},[activeId,workspace,mode])
  async function upload(file:File){const target=path==='.'?file.name:`${path}/${file.name}`;const send=async(approved:boolean)=>{const form=new FormData();form.append('file',file);const query=new URLSearchParams({workspace,path:target,permission_mode:mode,approved:String(approved)});return fetch(`${API_BASE}/api/files/upload?${query}`,{method:'POST',body:form})};try{const response=await send(mode!=='ask');const result=await response.json();if(result.status==='confirmation_required'){if(!confirm(`上传 ${file.name} 到 ${target}？`))return;await send(true)}load()}catch(caught){setError((caught as Error).message)}}
  async function undo(){const approved=mode==='full'||confirm('撤销最近一次文件修改？');if(!approved)return;try{await api('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:active?.id,workspace,permission_mode:mode,tool:'undo_file_change',arguments:{},approved:true})});load()}catch(caught){setError((caught as Error).message)}}
  return <section className="content-panel"><PanelHeader icon={<Files/>} title="工作区文件" subtitle={workspace}/><div className="panel-toolbar"><button className="primary" onClick={()=>fileRef.current?.click()}><Upload size={16}/>上传文件</button><input ref={fileRef} hidden type="file" onChange={event=>event.target.files?.[0]&&upload(event.target.files[0])}/><button className="secondary" onClick={undo}><RotateCcw size={16}/>撤销</button><button className="secondary" onClick={()=>load()}><Settings2 size={16}/>刷新</button></div>{error&&<p className="panel-error">{error}</p>}<div className="file-list">{path!=='.'&&<button onClick={()=>load(path.split('/').slice(0,-1).join('/')||'.')}><Folder/><span>..</span></button>}{items.map(item=><button key={item.path} onClick={()=>item.type==='directory'&&load(item.path)}><span className="file-icon">{item.type==='directory'?<Folder/>:<File/>}</span><span>{item.name}</span><small>{item.size?`${Math.ceil(item.size/1024)} KB`:''}</small>{item.type==='directory'&&<ChevronRight/>}</button>)}</div></section>
}
