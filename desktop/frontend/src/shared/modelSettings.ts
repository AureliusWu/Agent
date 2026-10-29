import type { ProviderConfiguration, ProviderModelDirectory, ProviderPolicy } from '../types'
import { credentialDestination, isLocalModelProvider, modelCredentialAlias } from './modelCredentials'
export { isLocalModelProvider } from './modelCredentials'

export function assertCredentialDestination(configuration: ProviderConfiguration, previous: ProviderConfiguration | null, keyDraft: string): void {
  if (isLocalModelProvider(configuration) || keyDraft.trim()) return
  if (!previous || credentialDestination(configuration) !== credentialDestination(previous)) {
    throw new Error('提供商或 API 地址已改变。为避免把原密钥发送到新地址，请填入新提供商对应的 API Key；原有密钥不会自动发送。')
  }
}

export function modelDirectoryRequest(configuration: ProviderConfiguration, keyDraft: string, previous: ProviderConfiguration | null = configuration): RequestInit {
  assertCredentialDestination(configuration, previous, keyDraft)
  const local = isLocalModelProvider(configuration)
  const headers: Record<string, string> = local ? { 'X-Siyi-Omit-Model-Credential': '1' } : {}
  if (!local && keyDraft.trim()) headers['X-Model-Api-Key'] = keyDraft.trim()
  return { method: 'POST', headers, body: JSON.stringify(configuration) }
}

export function availableChatModels(policy: ProviderPolicy | null): string[] {
  return [...new Set([
    policy?.provider.default_model,
    ...(policy?.provider.models || []),
    ...Object.values(policy?.models || {}),
  ].filter((model): model is string => Boolean(model)))]
}

export async function saveModelSettings(
  configuration: ProviderConfiguration,
  keyDraft: string,
  saveConfiguration: (value: ProviderConfiguration) => Promise<ProviderConfiguration>,
  saveKey: (name: string, value: string) => Promise<void>,
  previousConfiguration: ProviderConfiguration | null = configuration,
): Promise<ProviderConfiguration> {
  assertCredentialDestination(configuration, previousConfiguration, keyDraft)
  // An empty field means keep the existing Credential Manager entry, never delete it.
  if (!isLocalModelProvider(configuration) && keyDraft.trim()) {
    try { await saveKey(await modelCredentialAlias(configuration), keyDraft.trim()) }
    catch { throw new Error('API Key 未能保存到 Windows 凭据管理器，模型配置没有改变。原有密钥未删除，请重试。') }
  }
  try { return await saveConfiguration(configuration) }
  catch (error) {
    throw new Error(keyDraft.trim() && !isLocalModelProvider(configuration)
      ? `目标地址的密钥已保存，但模型配置未保存。原提供商配置保持不变，请检查后重试：${(error as Error).message}`
      : `模型配置未保存：${(error as Error).message}`)
  }
}

export function modelConnectionMessage(result: ProviderModelDirectory): string {
  if (result.status === 'ok') return `连接成功 · 获取 ${result.models.length} 个模型 · ${result.latency_ms ?? '—'} ms。仅验证模型目录，未执行付费对话。`
  const messages: Record<string, string> = {
    unconfigured: '尚未提供 API Key。可以先保存密钥，再检查连接。',
    missing_api_key: '尚未提供 API Key。可以先保存密钥，再检查连接。',
    authentication_error: 'API Key 未通过认证，请核对密钥及提供商。',
    authentication_failed: 'API Key 未通过认证，请核对密钥及提供商。',
    permission_denied: '提供商拒绝访问，请检查 API Key 的权限。',
    rate_limited: '提供商限流，请稍后重试；本次不会自动重试。',
    runtime_file_missing: '本地 HTTPS 运行文件缺失，请更新完整司忆程序；重新填写密钥不能解决。',
    tls_error: 'HTTPS 证书验证失败，请检查系统时间、代理或证书环境。',
    connection_error: '无法连接提供商，请检查网络、代理及 API 地址。',
    timeout: '连接超时，请检查网络后手动重试。',
    model_not_found: '连接已建立，但当前模型不在返回目录中；请选择目录中的模型。',
  }
  return messages[result.error_type || result.status] || result.error || '连接失败，请检查 API 地址和网络。'
}
