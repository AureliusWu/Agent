import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const localPython = fileURLToPath(new URL('../../../siyi/.venv/Scripts/python.exe', import.meta.url))
const python = process.env.PYTHON || (existsSync(localPython) ? localPython : 'python')
const generator = fileURLToPath(new URL('../../../scripts/generate_build_info.py', import.meta.url))

execFileSync(python, [generator, '--build-type', 'Release', '--respect-lock'], {
  stdio: 'inherit',
})
