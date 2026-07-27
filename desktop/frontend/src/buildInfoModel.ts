export interface BuildManifest {
  manifest_version: number
  product_version: string
  git_commit: string
  git_short_commit: string
  git_branch: string
  build_time: string
  build_type: 'Development' | 'Release'
  workspace_state: 'CLEAN' | 'DIRTY' | 'UNKNOWN'
  source_fingerprint: string
  build_id: string
  component_build_ids: { tauri: string; react: string; sidecar: string }
  database_schema_version: number
  release_status: {
    target_version: string
    source_version: string
    implementation_status: string
    test_status: string
    distribution_status: string
  }
  evidence_manifest_hash: string
  embedded?: boolean
  component?: string
  component_build_id?: string
}

export interface BuildConsistency {
  status: 'consistent' | 'mismatch' | 'incomplete'
  consistent: boolean | null
  missing: string[]
  buildIds: Record<string, string>
}

export function evaluateBuildConsistency(
  react: BuildManifest,
  desktop: BuildManifest | null,
  sidecar: BuildManifest | null,
  requireDesktop = true,
): BuildConsistency {
  const components: Record<string, BuildManifest | null> = { react, sidecar }
  if (requireDesktop) components.desktop = desktop
  const missing = Object.entries(components).filter(([, value]) => !value).map(([name]) => name)
  const buildIds = Object.fromEntries(
    Object.entries(components).filter(([, value]) => value).map(([name, value]) => [name, value!.build_id]),
  )
  if (missing.length) return { status: 'incomplete', consistent: null, missing, buildIds }
  const consistent = new Set(Object.values(buildIds)).size === 1
  return { status: consistent ? 'consistent' : 'mismatch', consistent, missing: [], buildIds }
}
