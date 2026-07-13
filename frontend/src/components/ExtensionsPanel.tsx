import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { FilePlus2, Gauge, Plug, Plus, Power, Shield, Sparkles, Trash2 } from 'lucide-react'
import { api } from '../api'
import { isDesktop, saveDesktopSecret } from '../secrets'
import type { ProviderHealth } from '../types'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

interface Skill {name:string;description:string;path:string;enabled:boolean}
interface Mcp {id:number;name:string;transport:string;url:string;enabled:number}

export function ExtensionsPanel({workspace}:{workspace:string}) {
  const [skills,setSkills]=useState<Skill[]>([]); const [mcps,setMcps]=useState<Mcp[]>([]); const [name,setName]=useState(''); const [url,setUrl]=useState(''); const [modelKey,setModelKey]=useState(''); const [keyStatus,setKeyStatus]=useState(''); const [health,setHealth]=useState<ProviderHealth|null>(null)
  const load=()=>{api<Skill[]>(`/api/skills?workspace=${encodeURIComponent(workspace)}`).then(setSkills).catch(()=>{});api<Mcp[]>('/api/mcp').then(setMcps).catch(()=>{})}
  useEffect(load,[workspace])
  async function addMcp(event:FormEvent){event.preventDefault();await api('/api/mcp',{method:'POST',body:JSON.stringify({name,transport:'http',url,args:[]})});setName('');setUrl('');load()}
  async function checkHealth(){try{setHealth(await api<ProviderHealth>('/api/provider/health'))}catch(caught){setHealth({status:'error',latency_ms:null,model:'',error:(caught as Error).message})}}
  async function saveKey(event:FormEvent){event.preventDefault();try{await saveDesktopSecret('model_api_key',modelKey);setModelKey('');setKeyStatus('已保存到 Windows 凭据管理器');setTimeout(checkHealth,150)}catch(caught){setKeyStatus((caught as Error).message)}}
  async function toggleSkill(item:Skill){await api(`/api/skills/enabled?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(item.path)}`,{method:'PATCH',body:JSON.stringify({enabled:!item.enabled})});load()}
  async function toggleMcp(item:Mcp){await api(`/api/mcp/${item.id}/enabled`,{method:'PATCH',body:JSON.stringify({enabled:!item.enabled})});load()}
  async function deleteMcp(id:number){if(!confirm('删除这个 MCP 服务？'))return;await api(`/api/mcp/${id}`,{method:'DELETE'});load()}
  return <section className="content-panel"><PanelHeader icon={<Plug/>} title="扩展能力" subtitle="模型、Skill 与 MCP"/><div className="extension-grid"><div><h3>模型凭据</h3><form className="mcp-form" onSubmit={saveKey}><input type="password" placeholder="DeepSeek / OpenAI-compatible API Key" value={modelKey} onChange={event=>setModelKey(event.target.value)} required/><button className="primary" disabled={!isDesktop()}><Shield size={16}/>{isDesktop()?'保存到 Windows 凭据':'网页端由后端环境变量管理'}</button>{keyStatus&&<p>{keyStatus}</p>}</form><button className="secondary health-button" onClick={checkHealth}><Gauge size={16}/>检查模型连接</button>{health&&<p className={`provider-health ${health.status}`}>{health.status==='ok'?`${health.model} 可用 · ${health.latency_ms} ms`:health.status==='unconfigured'?'尚未配置 API Key':`连接失败：${health.error}`}</p>}<h3>已挂载 Skill <span>{skills.length}</span></h3>{skills.length?skills.map(item=><div className={`extension-row ${item.enabled?'':'disabled'}`} key={item.path}><span><Sparkles/></span><div><strong>{item.name}</strong><p>{item.description||item.path}</p></div><button title={item.enabled?'停用':'启用'} onClick={()=>toggleSkill(item)}><Power size={15}/></button></div>):<div className="empty-panel"><FilePlus2/><p>在工作区 `.agent/skills/*/SKILL.md` 添加 Skill</p></div>}</div><div><h3>MCP 服务 <span>{mcps.length}</span></h3>{mcps.map(item=><div className={`extension-row ${item.enabled?'':'disabled'}`} key={item.id}><span><Plug/></span><div><strong>{item.name}</strong><p>{item.transport} · {item.url}</p></div><button title={item.enabled?'停用':'启用'} onClick={()=>toggleMcp(item)}><Power size={15}/></button><button title="删除" onClick={()=>deleteMcp(item.id)}><Trash2 size={15}/></button></div>)}<form className="mcp-form" onSubmit={addMcp}><input placeholder="服务名称" value={name} onChange={event=>setName(event.target.value)} required/><input placeholder="https://mcp.example.com/mcp" value={url} onChange={event=>setUrl(event.target.value)} required/><button className="primary"><Plus size={16}/>连接 HTTP MCP</button></form></div></div></section>
}
