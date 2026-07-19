import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

async function removeLegacyDesktopPwaCaches(): Promise<void> {
  if (!('__TAURI_INTERNALS__' in window)) return
  if ('serviceWorker' in navigator) {
    const registrations = await navigator.serviceWorker.getRegistrations()
    await Promise.all(registrations.map(registration => registration.unregister()))
  }
  if ('caches' in window) {
    const names = await caches.keys()
    await Promise.all(names.map(name => caches.delete(name)))
  }
}

void removeLegacyDesktopPwaCaches().catch(error => {
  if (import.meta.env.DEV) console.warn('Unable to remove legacy desktop PWA cache', error)
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
