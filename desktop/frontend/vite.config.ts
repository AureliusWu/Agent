import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string }
const buildManifestPath = resolve(process.env.SIYI_BUILD_MANIFEST || fileURLToPath(new URL('../../build/generated/build-info.json', import.meta.url)))
if (!existsSync(buildManifestPath)) {
  const python = process.env.PYTHON || fileURLToPath(new URL('../../siyi/.venv/Scripts/python.exe', import.meta.url))
  execFileSync(python, ['../../scripts/generate_build_info.py', '--build-type', 'Release'], { cwd: fileURLToPath(new URL('.', import.meta.url)), stdio: 'inherit' })
}
const buildInfo = JSON.parse(readFileSync(buildManifestPath, 'utf8'))
export default defineConfig(() => {
  return {
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    __BUILD_INFO__: JSON.stringify(buildInfo),
  },
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  }
})
