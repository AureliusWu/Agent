import { execSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

const packageJson = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string }
const gitCommit = (() => {
  try {
    return execSync('git rev-parse --short HEAD', { encoding: 'utf8' }).trim()
  } catch {
    return 'unknown'
  }
})()
const buildTime = new Date().toISOString()
export default defineConfig(({ mode }) => {
  const isDesktopBuild = mode === 'desktop' || Boolean(process.env.TAURI_ENV_PLATFORM)

  return {
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
    __BUILD_TIME__: JSON.stringify(buildTime),
    __GIT_COMMIT__: JSON.stringify(gitCommit),
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
