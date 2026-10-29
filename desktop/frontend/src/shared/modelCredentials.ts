import type { ProviderConfiguration } from '../types'

export function isLocalModelProvider(configuration: ProviderConfiguration): boolean {
  if (configuration.provider_id === 'ollama' || configuration.provider_id === 'mock') return true
  if (configuration.provider_id !== 'openai_compatible') return false
  try { return ['localhost', '127.0.0.1', '::1', '[::1]'].includes(new URL(configuration.base_url).hostname) }
  catch { return false }
}

export function credentialDestination(configuration: ProviderConfiguration): string {
  if (isLocalModelProvider(configuration)) return 'local:no-cloud-credentials'
  const url = new URL(configuration.base_url || (configuration.provider_id === 'deepseek' ? 'https://api.deepseek.com' : ''))
  if (url.username || url.password || url.search || url.hash) throw new Error('API 地址不能包含密钥、用户名或查询参数。')
  if (configuration.provider_id === 'deepseek' && url.protocol === 'https:' && url.hostname === 'api.deepseek.com' && (!url.port || url.port === '443') && /^\/(?:v1\/?)?$/.test(url.pathname)) return 'deepseek:https://api.deepseek.com'
  return `${configuration.provider_id}:${url.origin}${url.pathname.replace(/\/+$/, '')}`
}

export async function modelCredentialAlias(configuration: ProviderConfiguration): Promise<string> {
  const bytes = new TextEncoder().encode(credentialDestination(configuration))
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return `model_api_key.${[...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('')}`
}

export function mayUseLegacyModelCredential(configuration: ProviderConfiguration): boolean {
  return configuration.provider_id === 'deepseek' && credentialDestination(configuration) === 'deepseek:https://api.deepseek.com'
}
