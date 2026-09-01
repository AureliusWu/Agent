export interface MemoryOwnership {
  owner_api?: 'scoped' | 'long_term'
  editable_via_current_api?: boolean
}

export function canManageMemoryHere(
  memory: MemoryOwnership,
  currentApi: 'scoped' | 'long_term',
): boolean {
  // Fail closed when an older or malformed response omits the ownership
  // contract.  A mutation must never be guessed from the rendered shape.
  return memory.owner_api === currentApi && memory.editable_via_current_api === true
}

export function memoryCategoryLabel(
  memory: MemoryOwnership,
  scopedCategoryLabel?: string,
): string {
  if (memory.owner_api === 'long_term') return '长期记忆'
  return scopedCategoryLabel || '其他记忆'
}

export function memoryOwnerHint(memory: MemoryOwnership): string {
  return memory.owner_api === 'long_term'
    ? '请在“长期记忆”中管理'
    : '请在“我的记忆”中管理'
}
