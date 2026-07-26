import { useEffect, useRef, useState } from 'react'
import { ChevronRight, File, Files, Folder, RotateCcw, Settings2, Upload } from 'lucide-react'
import { api, apiFetch } from '../../api'
import type { Conversation, FileItem, PermissionMode } from '../../types'
import { PanelHeader } from '../shared/PanelHeader'
import '../../styles/panels.css'

export function FilesPanel({active, workspace, mode}: {active: Conversation|null; workspace:string; mode:PermissionMode}) {
  const [items,setItems]=useState<FileItem[]>([])
  const [path,setPath]=useState('.')
  const [error,setError]=useState('')
  const fileRef=useRef<HTMLInputElement>(null)
  const activeId = active?.id
  const load=async(next=path)=>{try{const result=await api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:active?.id,workspace,permission_mode:mode,tool:'list_files',arguments:{path:next}})});setItems(result.items);setPath(next);setError('')}catch(caught){setError((caught as Error).message)}}
  useEffect(()=>{if(!activeId)return;api<{items:FileItem[]}>('/api/tools/execute',{method:'POST',body:JSON.stringify({conversation_id:activeId,workspace,permission_mode:mode,tool:'list_files',arguments:{path:'.'}})}).then(result=>{setItems(result.items);setPath('.');setError('')}).catch(caught=>setError(caught.message))},[activeId,workspace,mode])
  async function upload(file:File){const target=path==='.'?file.name:`${path}/${file.name}`;const send=async(token?:string)=>{const form=new FormData();form.append('file',file);const query=new URLSearchParams({workspace,path:target,permission_mode:mode,conversation_id:String(active?.id||'')});if(token)query.set('approval_token',token);return apiFetch(`/api/files/upload?${query}`,{method:'POST',body:form})};try{let response=await send();let result=await response.json();if(result.status==='confirmation_required'){if(!confirm(`上传 ${file.name} 到 ${target}？`))return;response=await send(result.approval_key);result=await response.json()}if(result.status!=='ok')throw new Error(result.error||'上传失败');load()}catch(caught){setError((caught as Error).message)}}
  async function undo(){try{const body=(token?:string)=>JSON.stringify({conversation_id:active?.id,workspace,permission_mode:mode,tool:'undo_file_change',arguments:{},approval_tokens:token?[token]:[]});let result=await api<Record<string,unknown>>('/api/tools/execute',{method:'POST',body:body()});if(result.status==='confirmation_required'){if(!confirm('撤销最近一次文件修改？'))return;result=await api('/api/tools/execute',{method:'POST',body:body(String(result.approval_key))})}if(result.status!=='ok')throw new Error(String(result.error||'撤销失败'));load()}catch(caught){setError((caught as Error).message)}}
  return <section className="content-panel"><PanelHeader icon={<Files/>} title="工作区文件" subtitle={workspace}/><div className="panel-toolbar"><button className="primary" onClick={()=>fileRef.current?.click()}><Upload size={16}/>上传文件</button><input ref={fileRef} hidden type="file" onChange={event=>event.target.files?.[0]&&upload(event.target.files[0])}/><button className="secondary" onClick={undo}><RotateCcw size={16}/>撤销</button><button className="secondary" onClick={()=>load()}><Settings2 size={16}/>刷新</button></div>{error&&<p className="panel-error">{error}</p>}<div className="file-list">{path!=='.'&&<button onClick={()=>load(path.split('/').slice(0,-1).join('/')||'.')}><Folder/><span>..</span></button>}{items.map(item=><button key={item.path} onClick={()=>item.type==='directory'&&load(item.path)}><span className="file-icon">{item.type==='directory'?<Folder/>:<File/>}</span><span>{item.name}</span><small>{item.size?`${Math.ceil(item.size/1024)} KB`:''}</small>{item.type==='directory'&&<ChevronRight/>}</button>)}</div></section>
}
