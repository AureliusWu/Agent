import type { CommandDefinition } from '../types'

export function matchingCommands(input: string, commands: CommandDefinition[]): CommandDefinition[] {
  const normalized = input.trimStart()
  if (!normalized.startsWith('/') || normalized.includes(' ')) return []
  const prefix = normalized.toLocaleLowerCase('zh-CN')
  return commands.filter(item => item.name.startsWith(prefix))
}

export function commandDisabledReason(command: CommandDefinition, state: { busy: boolean; hasConversation: boolean; hasWorkspace?: boolean }): string {
  if (command.requires_conversation && !state.hasConversation) return '需要先创建或选择对话'
  if (command.requires_workspace && !state.hasWorkspace) return '需要先打开工作区'
  if (state.busy && !command.allowed_while_busy) return '任务运行中不可用'
  if (command.name === '/stop' && !state.busy) return '当前没有运行任务'
  return ''
}

export function completedCommandText(command: CommandDefinition): string {
  return `${command.name}${command.accepts_arguments ? ' ' : ''}`
}

export function moveCommandIndex(current: number, length: number, direction: 1 | -1): number {
  if (!length) return 0
  return (current + direction + length) % length
}
