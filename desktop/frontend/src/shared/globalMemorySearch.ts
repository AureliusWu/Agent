import type { GlobalMemorySearchItem } from '../types'

export function groupGlobalMemoryResults(items: GlobalMemorySearchItem[]) {
  return {
    global: items.filter(item => item.search_scope === 'global'),
    project: items.filter(item => item.search_scope === 'project'),
  }
}
