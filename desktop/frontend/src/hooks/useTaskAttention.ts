import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { onDesktopEndpointChange } from '../desktopRuntime'
import { parseTaskAttention } from '../shared/taskAttention'
import type { TaskAttentionSnapshot } from '../shared/taskAttention'

interface State {
  snapshot: TaskAttentionSnapshot | null
  loading: boolean
  failed: boolean
}

const loading: State = { snapshot: null, loading: true, failed: false }
const refreshIntervalMs = 10_000

// This hook is mounted only for the open attention tab. A closed tab, changed
// workbench scope or replaced sidecar cannot publish a late response.
export function useTaskAttention(activityRevision: string) {
  const [state, setState] = useState<State>(loading)
  const request = useRef<AbortController | null>(null)
  const refresh = useCallback(async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setState(old => ({ ...old, loading: true, failed: false }))
    try {
      const response = await api<unknown>('/api/tasks/attention', { signal: controller.signal })
      if (request.current !== controller || controller.signal.aborted) return
      setState({ snapshot: parseTaskAttention(response), loading: false, failed: false })
    } catch {
      if (request.current === controller && !controller.signal.aborted) setState(old => ({ ...old, loading: false, failed: true }))
    } finally {
      if (request.current === controller) request.current = null
    }
  }, [])

  useEffect(() => {
    const unsubscribe = onDesktopEndpointChange(() => {
      request.current?.abort()
      request.current = null
      setState({ snapshot: null, loading: false, failed: true })
    })
    const timer = window.setInterval(() => { if (!request.current) void refresh() }, refreshIntervalMs)
    return () => {
      unsubscribe()
      window.clearInterval(timer)
      request.current?.abort()
      request.current = null
    }
  }, [refresh])

  useEffect(() => { void refresh() }, [refresh, activityRevision])
  return { ...state, refresh }
}
