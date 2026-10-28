import assert from 'node:assert/strict'
import { captureEditSelection, runEditAction, runWindowAction } from '../src/shared/windowChrome.ts'
import type { WindowControls } from '../src/shared/windowChrome.ts'

const windowCalls: string[] = []
const targetWindow: WindowControls = {
  async minimize() { windowCalls.push('minimize') },
  async toggleMaximize() { windowCalls.push('maximize') },
  async isMaximized() { windowCalls.push('isMaximized'); return true },
  async close() { windowCalls.push('close') },
}
assert.equal(await runWindowAction(targetWindow, 'minimize'), null)
assert.deepEqual(windowCalls.splice(0), ['minimize'])
assert.equal(await runWindowAction(targetWindow, 'maximize'), true)
assert.deepEqual(windowCalls.splice(0), ['maximize', 'isMaximized'])
assert.equal(await runWindowAction(targetWindow, 'close'), null)
assert.deepEqual(windowCalls.splice(0), ['close'])
await assert.rejects(runWindowAction({ ...targetWindow, async close() { throw new Error('denied') } }, 'close'), /denied/)

function editorFixture() {
  const events: Event[] = []
  const editor = {
    tagName: 'TEXTAREA', value: 'abcde', selectionStart: 1, selectionEnd: 3,
    isConnected: true, disabled: false, readOnly: false,
    focus() {},
    setSelectionRange(start: number, end: number) { editor.selectionStart = start; editor.selectionEnd = end },
    select() { editor.selectionStart = 0; editor.selectionEnd = editor.value.length },
    setRangeText(text: string, start: number, end: number) { editor.value = editor.value.slice(0, start) + text + editor.value.slice(end) },
    dispatchEvent(event: Event) { events.push(event); return true },
  }
  const commands: string[] = []
  const document = {
    activeElement: editor,
    querySelector() { return editor },
    getSelection() { return { toString() { return '' } } },
    execCommand(command: string) { commands.push(command); return false },
  } as unknown as Document
  return { editor, events, commands, document }
}

let reads = 0
const writes: string[] = []
const clipboard = { async readText() { reads += 1; return 'X' }, async writeText(text: string) { writes.push(text) } }
const selectionFixture = editorFixture()
const selected = captureEditSelection(selectionFixture.document)
assert.equal(selected.text, 'bc')
assert.equal(reads, 0, 'capturing a selection must not read the clipboard')
assert.equal(await runEditAction('selectAll', selected, selectionFixture.document, clipboard), '')
assert.equal(reads, 0, 'select all must not read the clipboard')
assert.equal(selectionFixture.editor.selectionEnd, 5)

const pasteFixture = editorFixture()
assert.equal(await runEditAction('paste', captureEditSelection(pasteFixture.document), pasteFixture.document, clipboard), '')
assert.equal(reads, 1)
assert.equal(pasteFixture.editor.value, 'aXde')
assert.equal(pasteFixture.events[0].type, 'input', 'native fallback must synchronize the React input')

const cutFixture = editorFixture()
assert.equal(await runEditAction('cut', captureEditSelection(cutFixture.document), cutFixture.document, clipboard), '')
assert.equal(writes.at(-1), 'bc')
assert.equal(cutFixture.editor.value, 'ade')
assert.equal(reads, 1, 'cut must not read the clipboard')

const changedFixture = editorFixture()
let finishRead: (text: string) => void = () => {}
const delayedPaste = runEditAction('paste', captureEditSelection(changedFixture.document), changedFixture.document, {
  readText: () => new Promise<string>(resolve => { finishRead = resolve }), async writeText() {},
})
changedFixture.editor.value = 'new user text'
finishRead('do not overwrite')
assert.match(await delayedPaste, /已变化/)
assert.equal(changedFixture.editor.value, 'new user text')
assert.equal(changedFixture.events.length, 0)

const passwordDocument = {
  activeElement: { tagName: 'INPUT', type: 'password', get value() { throw new Error('password value must not be read') } },
  querySelector() { return selectionFixture.editor },
  getSelection() { return { toString() { return '' } } },
} as unknown as Document
assert.equal(captureEditSelection(passwordDocument).editor, null)
assert.equal(reads, 1)
console.log('Window controls and deliberate, race-safe editor menu checks passed')
