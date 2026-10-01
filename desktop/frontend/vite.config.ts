import { execFileSync } from 'node:child_process'
import { existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs'
import { isAbsolute, relative, resolve, sep } from 'node:path'
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
// An environment value avoids the nested Tauri/npm Windows quoting boundary.
// Only the fresh, dedicated candidate output may opt into emptyOutDir.
const candidateOutput = process.env.SIYI_CANDIDATE_FRONTEND_DIST
let candidateBuild: { outDir: string; emptyOutDir: true } | undefined
if (candidateOutput) {
  const candidatesRoot = fileURLToPath(new URL('../../build/candidates', import.meta.url))
  const outDir = resolve(candidateOutput)
  const childPath = relative(candidatesRoot, outDir)
  const parts = childPath.split(sep)
  if (!isAbsolute(candidateOutput) || isAbsolute(childPath) || parts.length !== 2
    || parts.some(part => !part || part === '.' || part === '..') || parts[1] !== 'frontend-dist') {
    throw new Error('Candidate frontend output must be build/candidates/<candidate>/frontend-dist')
  }
  let ancestor = outDir
  while (true) {
    if (existsSync(ancestor)) {
      const stat = lstatSync(ancestor)
      if (!stat.isDirectory() || stat.isSymbolicLink() || relative(ancestor, realpathSync(ancestor)) !== '') {
        throw new Error('Candidate frontend output must have ordinary directory ancestors')
      }
    }
    const parent = resolve(ancestor, '..')
    if (parent === ancestor) break
    ancestor = parent
  }
  candidateBuild = { outDir, emptyOutDir: true }
}
export default defineConfig(() => {
  return {
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    __BUILD_INFO__: JSON.stringify(buildInfo),
  },
  plugins: [react()],
  build: candidateBuild,
  server: { port: 5173, strictPort: true },
  }
})
