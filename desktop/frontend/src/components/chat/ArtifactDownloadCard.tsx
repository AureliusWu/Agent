import { Download, FileText, LoaderCircle } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { apiFetch } from '../../api'
import {
  artifactDownloadFromUrl,
  artifactTypeLabel,
  saveArtifactDownload,
  type ArtifactDownload,
} from '../../shared/artifactDownloads'

interface Props {
  artifact: ArtifactDownload
  compact?: boolean
  children?: ReactNode
}

export function ArtifactDownloadButton({ artifact, compact = false, children }: Props) {
  const [state, setState] = useState<'idle' | 'downloading' | 'failed'>('idle')
  const [error, setError] = useState('')

  async function download(): Promise<void> {
    if (state === 'downloading') return
    setState('downloading')
    setError('')
    try {
      await saveArtifactDownload(artifact, apiFetch)
      setState('idle')
    } catch (caught) {
      setState('failed')
      setError((caught as Error).message || '下载失败')
    }
  }

  return <>
    <button
      type="button"
      className={compact ? 'artifact-inline-download' : 'artifact-download-button'}
      disabled={state === 'downloading'}
      onClick={() => void download()}
      aria-label={`安全下载 ${artifact.filename}`}
    >
      {state === 'downloading' ? <LoaderCircle className="artifact-download-spinner" size={15} /> : <Download size={15} />}
      {children || (state === 'downloading' ? '下载中…' : state === 'failed' ? '重试下载' : '安全下载')}
    </button>
    {error && <span className="artifact-download-error" role="alert">{error}</span>}
  </>
}

export function ArtifactDownloadCard({ artifact }: { artifact: ArtifactDownload }) {
  return <section className="artifact-download-card" aria-label={`${artifactTypeLabel(artifact.mediaType, artifact.filename)}下载`}>
    <FileText size={22} aria-hidden="true" />
    <div className="artifact-download-copy">
      <strong>{artifact.filename}</strong>
      <span>{artifactTypeLabel(artifact.mediaType, artifact.filename)}</span>
      {artifact.path && artifact.path !== artifact.filename && <small title={artifact.path}>{artifact.path}</small>}
    </div>
    <ArtifactDownloadButton artifact={artifact} />
  </section>
}

export function AuthenticatedArtifactLink({ href, children }: { href: string; children?: ReactNode }) {
  const label = typeof children === 'string' ? children : ''
  const artifact = artifactDownloadFromUrl(href, label)
  if (!artifact) return <a href={href}>{children}</a>
  return <ArtifactDownloadButton artifact={artifact} compact>{children || '下载产物'}</ArtifactDownloadButton>
}
