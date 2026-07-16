import { useEffect, useMemo, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { api } from './api'
import { evaluateBuildConsistency } from './buildInfoModel'
import type { BuildConsistency, BuildManifest } from './buildInfoModel'

export type RuntimeEnvironment = 'Desktop' | 'Web'

interface DiagnosticStatus {
  build: BuildManifest
  database: { schema_version: number; expected_schema_version: number; status: string }
}

export interface BuildInfo {
  version: string
  environment: RuntimeEnvironment
  commit: string
  buildTime: string
  react: BuildManifest
  desktop: BuildManifest | null
  sidecar: BuildManifest | null
  databaseSchemaVersion: number | null
  consistency: BuildConsistency
}

function isTauriRuntime(): boolean {
  return '__TAURI_INTERNALS__' in window
}

export function useBuildInfo(): BuildInfo {
  const environment: RuntimeEnvironment = isTauriRuntime() ? 'Desktop' : 'Web'
  const [desktop, setDesktop] = useState<BuildManifest | null>(null)
  const [sidecar, setSidecar] = useState<BuildManifest | null>(null)
  const [databaseSchemaVersion, setDatabaseSchemaVersion] = useState<number | null>(null)

  useEffect(() => {
    let active = true
    async function refresh() {
      const [desktopResult, diagnosticResult] = await Promise.allSettled([
        environment === 'Desktop' ? invoke<BuildManifest>('desktop_build_info') : Promise.resolve(null),
        api<DiagnosticStatus>('/api/diagnostics/status'),
      ])
      if (!active) return
      setDesktop(desktopResult.status === 'fulfilled' ? desktopResult.value : null)
      if (diagnosticResult.status === 'fulfilled') {
        setSidecar(diagnosticResult.value.build)
        setDatabaseSchemaVersion(diagnosticResult.value.database.schema_version)
      } else {
        setSidecar(null)
        setDatabaseSchemaVersion(null)
      }
    }
    void refresh()
    const timer = window.setInterval(refresh, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [environment])

  const consistency = useMemo(
    () => evaluateBuildConsistency(__BUILD_INFO__, desktop, sidecar, environment === 'Desktop'),
    [desktop, environment, sidecar],
  )
  return {
    version: __APP_VERSION__,
    environment,
    commit: __BUILD_INFO__.git_short_commit,
    buildTime: __BUILD_INFO__.build_time,
    react: __BUILD_INFO__,
    desktop,
    sidecar,
    databaseSchemaVersion,
    consistency,
  }
}
