import assert from 'node:assert/strict'
import { composerRoute } from '../src/commands/commandRoute.ts'

for (const command of ['/help', '/unknown', '/memory private words', '/stop']) {
  assert.equal(composerRoute(command, { busy: false }), 'command')
  assert.equal(composerRoute(command, { busy: true }), 'command')
  assert.equal(composerRoute(command, { busy: true, steer: true }), 'command')
}
assert.equal(composerRoute('normal task', { busy: false }), 'task')
assert.equal(composerRoute('normal task', { busy: true }), 'queue')
assert.equal(composerRoute('normal steer', { busy: true, steer: true }), 'steer')
assert.equal(composerRoute('/help', { busy: false, existingTaskId: 'resume-id' }), 'resume')
for (const spokenCommand of ['/stop', '/clear', '/memory private words']) {
  assert.equal(composerRoute(spokenCommand, { busy: false, voiceSessionId: 'voice-session' }), 'task')
  assert.equal(composerRoute(spokenCommand, { busy: true, voiceSessionId: 'voice-session' }), 'queue')
}

console.log('Command routing zero-model boundary tests passed')
