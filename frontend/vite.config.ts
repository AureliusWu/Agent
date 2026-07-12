import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), VitePWA({
    registerType: 'autoUpdate',
    manifest: {
      name: 'Agent', short_name: 'Agent', description: '工作区沙箱通用 Agent',
      theme_color: '#1f2b27', background_color: '#fbfcfa', display: 'standalone',
      icons: [
        { src: '/pwa-192.png', sizes: '192x192', type: 'image/png' },
        { src: '/pwa-512.png', sizes: '512x512', type: 'image/png' },
      ],
    },
    workbox: { navigateFallback: '/index.html', runtimeCaching: [{ urlPattern: /^https?:\/\//, handler: 'NetworkFirst', options: { cacheName: 'agent-api', networkTimeoutSeconds: 5 } }] },
  })],
  server: { port: 5173 },
})
