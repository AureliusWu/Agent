export type WindowAction = 'minimize' | 'maximize' | 'close'

export interface WindowControls {
  minimize: () => Promise<void>
  toggleMaximize: () => Promise<void>
  isMaximized: () => Promise<boolean>
  close: () => Promise<void>
}

/** Only the explicitly selected window operation is dispatched. */
export async function runWindowAction(target: WindowControls, action: WindowAction): Promise<boolean | null> {
  if (action === 'minimize') { await target.minimize(); return null }
  if (action === 'close') { await target.close(); return null }
  await target.toggleMaximize()
  return target.isMaximized()
}

export type EditAction = 'undo' | 'redo' | 'cut' | 'copy' | 'paste' | 'selectAll'
type TextEditor = HTMLInputElement | HTMLTextAreaElement

export interface EditSelection { editor: TextEditor | null; start: number; end: number; text: string }

function isTextEditor(candidate: Element | null): candidate is TextEditor {
  if (!candidate) return false
  if (candidate.tagName === 'TEXTAREA') return true
  return candidate.tagName === 'INPUT' && ['text', 'search', 'url', 'tel'].includes((candidate as HTMLInputElement).type)
}

/** Capture only this window's selected text; never inspect password inputs. */
export function captureEditSelection(document: Document): EditSelection {
  const active = document.activeElement
  const fallback = document.querySelector('[data-composer-input]')
  const selectedText = document.getSelection()?.toString() || ''
  const passwordFocused = active?.tagName === 'INPUT' && (active as HTMLInputElement).type === 'password'
  const editor = isTextEditor(active) ? active : !selectedText && !passwordFocused && isTextEditor(fallback) ? fallback : null
  const start = editor?.selectionStart ?? 0
  const end = editor?.selectionEnd ?? start
  return { editor, start, end, text: isTextEditor(active) ? active.value.slice(start, end) : selectedText || editor?.value.slice(start, end) || '' }
}

interface ClipboardAccess { readText: () => Promise<string>; writeText: (text: string) => Promise<void> }

function restoreEditor(selection: EditSelection): TextEditor | null {
  const editor = selection.editor
  if (!editor?.isConnected || editor.disabled) return null
  editor.focus()
  editor.setSelectionRange(selection.start, selection.end)
  return editor
}

function replaceSelection(editor: TextEditor, text: string) {
  editor.setRangeText(text, editor.selectionStart ?? 0, editor.selectionEnd ?? 0, 'end')
  // The native value update plus a bubbling input event synchronizes React.
  editor.dispatchEvent(new Event('input', { bubbles: true }))
}

/** Clipboard access occurs only in response to a deliberate menu action. */
export async function runEditAction(action: EditAction, selection: EditSelection, document: Document, clipboard?: ClipboardAccess): Promise<string> {
  const editor = restoreEditor(selection)
  if (action === 'copy' && !editor) {
    if (!selection.text) return '请先选择要复制的文字。'
    if (!clipboard) return '请使用 Ctrl+C 复制所选文字。'
    await clipboard.writeText(selection.text)
    return ''
  }
  if (!editor) return '请先选择一个文本输入框。'
  if (action === 'selectAll') { editor.select(); return '' }
  if (action === 'copy') {
    const text = editor.value.slice(editor.selectionStart ?? 0, editor.selectionEnd ?? 0)
    if (!text) return '请先选择要复制的文字。'
    if (document.execCommand('copy')) return ''
    if (!clipboard) return '请使用 Ctrl+C 复制所选文字。'
    await clipboard.writeText(text)
    return ''
  }
  if (editor.readOnly) return '这个输入框是只读的。'
  if (action === 'paste') {
    if (!clipboard) return '请使用 Ctrl+V 粘贴文字。'
    const originalValue = editor.value
    const text = await clipboard.readText()
    if (!editor.isConnected) return '原输入框已关闭，请重新选择。'
    if (editor.readOnly || editor.disabled || editor.value !== originalValue) return '输入内容已变化，请重新选择粘贴位置。'
    editor.focus()
    editor.setSelectionRange(selection.start, selection.end)
    if (!document.execCommand('insertText', false, text)) replaceSelection(editor, text)
    return ''
  }
  if (action === 'cut') {
    const text = editor.value.slice(editor.selectionStart ?? 0, editor.selectionEnd ?? 0)
    if (!text) return '请先选择要剪切的文字。'
    if (document.execCommand('cut')) return ''
    if (!clipboard) return '请使用 Ctrl+X 剪切所选文字。'
    const originalValue = editor.value
    await clipboard.writeText(text)
    if (!editor.isConnected) return '原输入框已关闭，请重新选择。'
    if (editor.readOnly || editor.disabled || editor.value !== originalValue) return '输入内容已变化，请重新选择剪切位置。'
    editor.setSelectionRange(selection.start, selection.end)
    replaceSelection(editor, '')
    return ''
  }
  return document.execCommand(action) ? '' : action === 'undo' ? '没有可撤销的输入。' : '没有可重做的输入。'
}
