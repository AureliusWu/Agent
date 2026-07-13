import { useEffect, useState } from 'react'
import { History } from 'lucide-react'
import { api } from '../api'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

interface AuditLog {id:number;action:string;target:string;status:string;created_at:string}

export function AuditPanel(){
  const [logs,setLogs]=useState<AuditLog[]>([])
  useEffect(()=>{api<AuditLog[]>('/api/audit').then(setLogs).catch(()=>{})},[])
  return <section className="content-panel"><PanelHeader icon={<History/>} title="操作审计" subtitle="文件、模型与 MCP 调用记录"/><div className="audit-list">{logs.map(log=><div key={log.id}><span className={`audit-status ${log.status}`}/><div><strong>{log.action}</strong><p>{log.target||'系统操作'}</p></div><time>{new Date(log.created_at).toLocaleString()}</time><code>{log.status}</code></div>)}</div></section>
}
