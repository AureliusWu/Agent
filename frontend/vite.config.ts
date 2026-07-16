import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string }
const buildManifestPath = resolve(process.env.SIYI_BUILD_MANIFEST || fileURLToPath(new URL('../build/generated/build-info.json', import.meta.url)))
if (!existsSync(buildManifestPath)) {
  const python = process.env.PYTHON || fileURLToPath(new URL('../backend/.venv/Scripts/python.exe', import.meta.url))
  execFileSync(python, ['../scripts/generate_build_info.py', '--build-type', 'Release'], { cwd: fileURLToPath(new URL('.', import.meta.url)), stdio: 'inherit' })
}
const buildInfo = JSON.parse(readFileSync(buildManifestPath, 'utf8'))
export default defineConfig(({ mode }) => {
  const isDesktopBuild = mode === 'desktop' || Boolean(process.env.TAURI_ENV_PLATFORM)

  return {
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    __BUILD_INFO__: JSON.stringify(buildInfo),
  },
  plugins: [react(), VitePWA({
    disable: isDesktopBuild,
    registerType: 'autoUpdate',
    manifest: {
      name: '司忆',
      short_name: '司忆',
      description: '夏目心与司忆执行核心驱动的个人 Agent',
      theme_color: '#ffffff',
      background_color: '#ffffff',
      display: 'standalone',
      icons: [
        { src: '/pwa-192.png', sizes: '192x192', type: 'image/png' },
        { src: '/pwa-512.png', sizes: '512x512', type: 'image/png' },
      ],
    },
    workbox: { navigateFallback: '/index.html' },
  })],
  server: { port: 5173, strictPort: true },
  }
})
