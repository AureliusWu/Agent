import { useRef, useState } from 'react'
import { Download, RefreshCcw, Upload } from 'lucide-react'
import { apiFetch } from '../../api'

export function BackupPanel() {
  const fileRef = useRef<HTMLInputElement>(null)
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  async function exportBackup() {
    if (!confirm('完整备份包含对话、记忆、身份与状态，可能含敏感信息。确认导出并自行安全保管？')) return
    setBusy(true)
    setStatus('')
    try {
      const response = await apiFetch('/api/backups/complete?include_sensitive=true&administrator_confirmed=true')
      if (!response.ok) throw new Error((await response.json() as { detail?: string }).detail || '备份导出失败')
      const blob = await response.blob()
      const disposition = response.headers.get('content-disposition') || ''
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] || 'siyi-complete-backup.zip'
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = filename
      link.click()
      URL.revokeObjectURL(link.href)
      setStatus('完整备份已导出')
    } catch (caught) { setStatus((caught as Error).message) }
    finally { setBusy(false) }
  }

  async function restoreBackup(file: File) {
    if (!confirm('恢复会替换当前对话、记忆和状态。系统会先自动备份当前数据库，确认继续？')) return
    setBusy(true)
    setStatus('')
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('administrator_confirmed', 'true')
      const response = await apiFetch('/api/backups/complete/restore', { method: 'POST', body: form })
      const result = await response.json() as { restored?: boolean; safety_backup?: string; detail?: string }
      if (!response.ok) throw new Error(result.detail || '备份恢复失败')
      setStatus(`恢复完成；恢复前安全备份：${result.safety_backup}`)
    } catch (caught) { setStatus((caught as Error).message) }
    finally { setBusy(false) }
  }

  return <article className="about-card backup-card">
    <h3>完整备份与恢复</h3>
    <p>备份包含身份、对话、长期记忆、情绪、关系和任务状态。导入前会自动保存当前数据库，校验失败则不替换。</p>
    <div className="about-actions">
      <button className="primary" disabled={busy} onClick={exportBackup}><Download size={15} />导出完整备份</button>
      <button className="secondary" disabled={busy} onClick={() => fileRef.current?.click()}><Upload size={15} />恢复备份</button>
      <input ref={fileRef} hidden type="file" accept=".zip" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void restoreBackup(file) }} />
      {busy && <span className="secondary"><RefreshCcw size={15} />处理中</span>}
    </div>
    {status && <p className="backup-status" role="status">{status}</p>}
  </article>
}
