import assert from 'node:assert/strict'
import { commandDisabledReason, completedCommandText, matchingCommands, moveCommandIndex } from '../src/commands/commandMenu.ts'
import type { CommandDefinition } from '../src/types.ts'

const command = (name: string, values: Partial<CommandDefinition> = {}): CommandDefinition => ({
  name,
  title: name,
  description: name,
  usage: name,
  category: 'diagnostics',
  execution: 'frontend',
  risk: 'none',
  requires_argument: false,
  requires_conversation: false,
  requires_workspace: false,
  allowed_while_busy: false,
  accepts_arguments: false,
  ...values,
})
const commands = [command('/compact'), command('/context'), command('/stop', { allowed_while_busy: true }), command('/memory', { usage: '/memory <关键词>', requires_argument: true, accepts_arguments: true })]

assert.deepEqual(matchingCommands('/', commands).map(item => item.name), ['/compact', '/context', '/stop', '/memory'])
assert.deepEqual(matchingCommands('/co', commands).map(item => item.name), ['/compact', '/context'])
assert.deepEqual(matchingCommands('/memory term', commands), [])
assert.equal(completedCommandText(commands[3]), '/memory ')
assert.equal(moveCommandIndex(0, 4, -1), 3)
assert.equal(moveCommandIndex(3, 4, 1), 0)
assert.equal(commandDisabledReason(commands[0], { busy: true, hasConversation: true }), '任务运行中不可用')
assert.equal(commandDisabledReason(commands[2], { busy: true, hasConversation: true }), '')
assert.equal(commandDisabledReason(commands[2], { busy: false, hasConversation: true }), '当前没有运行任务')

console.log('Command menu contract tests passed')
