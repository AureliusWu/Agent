import { useEffect, useState } from 'react'
import { Fingerprint } from 'lucide-react'
import { api } from '../../api'

interface IdentityState {
  agent_id: string
  active_identity_version: number
  identity: { display_name: string; self_description: string; core_principles: string[] }
}

export function IdentityPanel() {
  const [identity, setIdentity] = useState<IdentityState | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { void api<IdentityState>('/api/identity').then(setIdentity).catch(caught => setError((caught as Error).message)) }, [])
  return <article className="about-card identity-card">
    <h3><Fingerprint size={16} />身份内核</h3>
    {error ? <p className="panel-error">{error}</p> : identity ? <>
      <dl><div><dt>身份</dt><dd>{identity.identity.display_name}</dd></div><div><dt>Agent ID</dt><dd>{identity.agent_id}</dd></div><div><dt>身份版本</dt><dd>{identity.active_identity_version}</dd></div></dl>
      <p>{identity.identity.self_description}</p>
    </> : <p>身份加载中…</p>}
  </article>
}
