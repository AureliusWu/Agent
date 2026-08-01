import assert from 'node:assert/strict'
import {
  artifactDownloadFromUrl,
  artifactTypeLabel,
  extractArtifactDownloads,
  filenameFromContentDisposition,
  saveArtifactDownload,
  type ArtifactDownloadPlatform,
} from '../src/shared/artifactDownloads.ts'

const artifactId = 'a'.repeat(64)
const [docx] = extractArtifactDownloads({
  tool: 'artifact.docx.create',
  result: {
    data: {
      artifact_id: artifactId,
      media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      download_url: 'https://attacker.example/steal',
      path: 'reports/季度报告.docx',
    },
  },
})
assert.equal(docx.artifactId, artifactId)
assert.equal(docx.downloadUrl, `/api/artifacts/${artifactId}/raw`)
assert.equal(docx.filename, '季度报告.docx')
assert.equal(artifactTypeLabel(docx.mediaType, docx.filename), 'Word 文档')

const [receiptOnly] = extractArtifactDownloads({
  tool: 'artifact.pdf.create',
  receipt: {
    artifact_id: artifactId,
    changed_files: ['output/report.pdf'],
  },
})
assert.equal(receiptOnly.mediaType, 'application/pdf')
assert.equal(receiptOnly.filename, 'report.pdf')

assert.equal(extractArtifactDownloads({ artifact_id: '../not-an-id' }).length, 0)
assert.equal(artifactDownloadFromUrl(`https://evil.example/api/artifacts/${artifactId}/raw`), null)
assert.equal(artifactDownloadFromUrl(`/api/artifacts/${artifactId}`)?.downloadUrl, `/api/artifacts/${artifactId}/raw`)
assert.equal(
  filenameFromContentDisposition("attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A%202026.pdf", 'fallback.pdf'),
  '报告 2026.pdf',
)
assert.equal(filenameFromContentDisposition('attachment; filename="../../evil.exe"', 'fallback.pdf'), 'evil.exe')

const calls: Array<{ path: string; options?: RequestInit }> = []
const lifecycle: string[] = []
const platform: ArtifactDownloadPlatform = {
  createObjectUrl: () => {
    lifecycle.push('create')
    return 'blob:test'
  },
  save: (url, filename) => lifecycle.push(`save:${url}:${filename}`),
  revokeObjectUrl: url => lifecycle.push(`revoke:${url}`),
}
const saved = await saveArtifactDownload(
  docx,
  async (path, options) => {
    calls.push({ path, options })
    return new Response(new Blob(['binary']), {
      status: 200,
      headers: { 'Content-Disposition': 'attachment; filename="server-report.docx"' },
    })
  },
  platform,
)
assert.equal(saved, 'server-report.docx')
assert.equal(calls[0].path, `/api/artifacts/${artifactId}/raw`)
assert.equal(new Headers(calls[0].options?.headers).get('Accept'), docx.mediaType)
assert.equal(calls[0].options?.cache, 'no-store')
assert.deepEqual(lifecycle, ['create', 'save:blob:test:server-report.docx', 'revoke:blob:test'])

const failingLifecycle: string[] = []
await assert.rejects(
  saveArtifactDownload(
    docx,
    async () => new Response('binary'),
    {
      createObjectUrl: () => 'blob:failure',
      save: () => {
        failingLifecycle.push('save')
        throw new Error('save failed')
      },
      revokeObjectUrl: url => failingLifecycle.push(`revoke:${url}`),
    },
  ),
  /save failed/,
)
assert.deepEqual(failingLifecycle, ['save', 'revoke:blob:failure'])

console.log('Artifact download tests passed')
