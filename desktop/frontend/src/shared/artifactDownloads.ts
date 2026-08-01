export interface ArtifactDownload {
  artifactId: string
  mediaType: string
  downloadUrl: string
  filename: string
  path?: string
}

export type ArtifactFetcher = (path: string, options?: RequestInit) => Promise<Response>

export interface ArtifactDownloadPlatform {
  createObjectUrl: (blob: Blob) => string
  revokeObjectUrl: (url: string) => void
  save: (url: string, filename: string) => void
}

const ARTIFACT_ID_PATTERN = /^[a-f0-9]{64}$/i
const ARTIFACT_URL_PATTERN = /^\/api\/artifacts\/([a-f0-9]{64})(?:\/raw)?(?:[?#].*)?$/i
const MAX_SCAN_DEPTH = 5
const MAX_SCAN_ITEMS = 100

const MEDIA_TYPE_BY_EXTENSION: Record<string, string> = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  md: 'text/markdown',
  markdown: 'text/markdown',
  pdf: 'application/pdf',
  png: 'image/png',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

const EXTENSION_BY_MEDIA_TYPE: Record<string, string> = {
  'application/pdf': 'pdf',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
  'image/png': 'png',
  'text/markdown': 'md',
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function basename(value: string): string {
  const parts = value.replaceAll('\\', '/').split('/')
  return parts.at(-1) || ''
}

function extension(value: string): string {
  const filename = basename(value)
  const index = filename.lastIndexOf('.')
  return index > 0 ? filename.slice(index + 1).toLowerCase() : ''
}

export function safeArtifactFilename(value: string, fallback: string): string {
  const withoutControlCharacters = [...basename(value)]
    .filter(character => {
      const codePoint = character.codePointAt(0) || 0
      return codePoint >= 32 && codePoint !== 127
    })
    .join('')
  const candidate = withoutControlCharacters
    .replace(/[<>:"/\\|?*]/g, '_')
    .trim()
    .slice(0, 180)
  if (!candidate || candidate === '.' || candidate === '..') return fallback
  return candidate
}

function canonicalArtifactUrl(artifactId: string): string {
  return `/api/artifacts/${artifactId}/raw`
}

function artifactIdFromUrl(value: string): string {
  const match = ARTIFACT_URL_PATTERN.exec(value)
  return match?.[1]?.toLowerCase() || ''
}

function mediaTypeFor(record: Record<string, unknown>, tool: string, path: string): string {
  const explicit = stringValue(record.media_type || record.mediaType).split(';', 1)[0].toLowerCase()
  if (explicit) return explicit
  const byExtension = MEDIA_TYPE_BY_EXTENSION[extension(path)]
  if (byExtension) return byExtension
  if (tool.includes('.docx.')) return MEDIA_TYPE_BY_EXTENSION.docx
  if (tool.includes('.pdf.')) return MEDIA_TYPE_BY_EXTENSION.pdf
  if (tool.includes('.pptx.')) return MEDIA_TYPE_BY_EXTENSION.pptx
  if (tool.includes('.markdown.')) return MEDIA_TYPE_BY_EXTENSION.md
  return 'application/octet-stream'
}

function firstChangedPath(record: Record<string, unknown>): string {
  const changed = record.changed_files || record.changedFiles
  if (!Array.isArray(changed)) return ''
  return stringValue(changed.find(value => typeof value === 'string'))
}

function descriptorFromRecord(record: Record<string, unknown>, inheritedTool: string): ArtifactDownload | null {
  const downloadUrl = stringValue(record.download_url || record.downloadUrl)
  const fromUrl = artifactIdFromUrl(downloadUrl)
  const rawArtifactId = stringValue(record.artifact_id || record.artifactId)
  const artifactId = (ARTIFACT_ID_PATTERN.test(rawArtifactId) ? rawArtifactId : fromUrl).toLowerCase()
  if (!artifactId) return null

  const tool = stringValue(record.tool) || inheritedTool
  const path = stringValue(record.path) || firstChangedPath(record)
  const mediaType = mediaTypeFor(record, tool, path)
  const fallbackExtension = EXTENSION_BY_MEDIA_TYPE[mediaType] || 'bin'
  const fallback = `${artifactId}.${fallbackExtension}`
  const filename = safeArtifactFilename(stringValue(record.filename) || path, fallback)

  return {
    artifactId,
    mediaType,
    downloadUrl: canonicalArtifactUrl(artifactId),
    filename,
    ...(path ? { path: path.replaceAll('\\', '/') } : {}),
  }
}

export function mergeArtifactDownloads(...groups: ReadonlyArray<ReadonlyArray<ArtifactDownload>>): ArtifactDownload[] {
  const merged = new Map<string, ArtifactDownload>()
  for (const group of groups) {
    for (const item of group) {
      const previous = merged.get(item.artifactId)
      merged.set(item.artifactId, previous
        ? {
            ...previous,
            ...item,
            filename: item.filename || previous.filename,
            mediaType: item.mediaType === 'application/octet-stream' ? previous.mediaType : item.mediaType,
            path: item.path || previous.path,
          }
        : item)
    }
  }
  return [...merged.values()]
}

export function extractArtifactDownloads(value: unknown, inheritedTool = ''): ArtifactDownload[] {
  const results: ArtifactDownload[] = []
  const seen = new Set<object>()
  let scanned = 0

  function visit(candidate: unknown, depth: number, tool: string): void {
    if (depth > MAX_SCAN_DEPTH || scanned >= MAX_SCAN_ITEMS || candidate === null || typeof candidate !== 'object') return
    if (seen.has(candidate)) return
    seen.add(candidate)
    scanned += 1

    if (Array.isArray(candidate)) {
      for (const item of candidate.slice(0, MAX_SCAN_ITEMS - scanned)) visit(item, depth + 1, tool)
      return
    }

    const record = candidate as Record<string, unknown>
    const localTool = stringValue(record.tool) || tool
    const descriptor = descriptorFromRecord(record, localTool)
    if (descriptor) results.push(descriptor)
    for (const key of ['data', 'result', 'receipt', 'artifact', 'artifacts']) {
      if (key in record) visit(record[key], depth + 1, localTool)
    }
  }

  visit(value, 0, inheritedTool)
  return mergeArtifactDownloads(results)
}

export function artifactDownloadFromUrl(href: string, filenameHint = ''): ArtifactDownload | null {
  const artifactId = artifactIdFromUrl(href)
  if (!artifactId) return null
  const inferredMediaType = MEDIA_TYPE_BY_EXTENSION[extension(filenameHint)] || 'application/octet-stream'
  const fallbackExtension = EXTENSION_BY_MEDIA_TYPE[inferredMediaType] || 'bin'
  return {
    artifactId,
    mediaType: inferredMediaType,
    downloadUrl: canonicalArtifactUrl(artifactId),
    filename: safeArtifactFilename(filenameHint, `${artifactId}.${fallbackExtension}`),
  }
}

export function artifactTypeLabel(mediaType: string, filename = ''): string {
  const normalized = mediaType.split(';', 1)[0].toLowerCase()
  if (normalized === MEDIA_TYPE_BY_EXTENSION.docx || extension(filename) === 'docx') return 'Word 文档'
  if (normalized === MEDIA_TYPE_BY_EXTENSION.pdf || extension(filename) === 'pdf') return 'PDF 文档'
  if (normalized === MEDIA_TYPE_BY_EXTENSION.pptx || extension(filename) === 'pptx') return 'PowerPoint 演示文稿'
  if (normalized === MEDIA_TYPE_BY_EXTENSION.md || ['md', 'markdown'].includes(extension(filename))) return 'Markdown 文档'
  if (normalized === MEDIA_TYPE_BY_EXTENSION.png || extension(filename) === 'png') return 'PNG 图片'
  return '文件'
}

export function filenameFromContentDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header)?.[1]
  if (encoded) {
    try {
      return safeArtifactFilename(decodeURIComponent(encoded.trim().replace(/^"|"$/g, '')), fallback)
    } catch {
      // Fall through to the plain filename form.
    }
  }
  const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/i.exec(header)
  return safeArtifactFilename((plain?.[1] || plain?.[2] || '').trim(), fallback)
}

function browserDownloadPlatform(): ArtifactDownloadPlatform {
  return {
    createObjectUrl: blob => URL.createObjectURL(blob),
    revokeObjectUrl: url => URL.revokeObjectURL(url),
    save: (url, filename) => {
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      link.rel = 'noopener'
      link.style.display = 'none'
      document.body.append(link)
      link.click()
      link.remove()
    },
  }
}

export async function saveArtifactDownload(
  artifact: ArtifactDownload,
  fetcher: ArtifactFetcher,
  platform: ArtifactDownloadPlatform = browserDownloadPlatform(),
): Promise<string> {
  const response = await fetcher(artifact.downloadUrl, {
    cache: 'no-store',
    headers: { Accept: artifact.mediaType || 'application/octet-stream' },
  })
  if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`)

  const filename = filenameFromContentDisposition(
    response.headers.get('Content-Disposition'),
    artifact.filename,
  )
  const blob = await response.blob()
  const objectUrl = platform.createObjectUrl(blob)
  try {
    platform.save(objectUrl, filename)
  } finally {
    platform.revokeObjectUrl(objectUrl)
  }
  return filename
}
