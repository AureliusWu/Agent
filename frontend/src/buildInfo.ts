import { useEffect, useState } from 'react'

export type RuntimeEnvironment = 'Desktop' | 'Web'

export interface BuildInfo {
  version: string
  environment: RuntimeEnvironment
  commit: string
  buildTime: string
}

function isTauriRuntime(): boolean {
  return '__TAURI_INTERNALS__' in window
}

export function useBuildInfo(): BuildInfo {
  const environment: RuntimeEnvironment = isTauriRuntime() ? 'Desktop' : 'Web'
  const [version, setVersion] = useState(__APP_VERSION__)

  useEffect(() => {
    if (environment !== 'Desktop') return
    import('@tauri-apps/api/app')
      .then(({ getVersion }) => getVersion())
      .then(setVersion)
      .catch(() => setVersion(__APP_VERSION__))
  }, [environment])

  return {
    version,
    environment,
    commit: __GIT_COMMIT__,
    buildTime: __BUILD_TIME__,
  }
}
