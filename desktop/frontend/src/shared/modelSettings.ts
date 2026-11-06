import type { ProviderConfiguration, ProviderModelDirectory, ProviderPolicy } from '../types'
import { credentialDestination, isLocalModelProvider, modelCredentialAlias, MODEL_CONFIGURATION_TOKEN } from './modelCredentials'
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
  const sameAudience = !local && previous && credentialDestination(configuration) === credentialDestination(previous)
  return { method: 'POST', headers, body: JSON.stringify({
    ...configuration,
    configuration_revision: undefined,
    credential_generation: sameAudience ? previous.credential_generation : undefined,
  }) }
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
  saveConfiguration: (value: ProviderConfiguration, expectedRevision: string) => Promise<ProviderConfiguration>,
  saveKey: (name: string, value: string) => Promise<void>,
  previousConfiguration: ProviderConfiguration | null = configuration,
  readConfiguration?: () => Promise<ProviderConfiguration>,
): Promise<ProviderConfiguration> {
  assertCredentialDestination(configuration, previousConfiguration, keyDraft)
  const revision = previousConfiguration?.configuration_revision
  // The field is present even for a legacy settings file when the sidecar
  // supports guarded saves. An older sidecar must not activate a staged key.
  if (typeof revision !== 'string' || !MODEL_CONFIGURATION_TOKEN.test(revision)) {
    throw new Error('本地核心尚未确认支持安全保存。请刷新模型设置后重试，原配置和密钥未修改。')
  }
  const local = isLocalModelProvider(configuration)
  const staged = !local && Boolean(keyDraft.trim())
  const generation = local ? undefined : staged
    ? [...crypto.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('')
    : previousConfiguration?.credential_generation
  if (staged && generation === previousConfiguration?.credential_generation) {
    throw new Error('候选凭据版本冲突，原配置和密钥未修改。')
  }
  const draft = { ...configuration, configuration_revision: undefined, credential_generation: generation }
  if (staged) {
    // The active key is never overwritten, read back, or deleted. Even if
    // native persistence succeeded but its acknowledgement failed, the new
    // slot remains inert unless the configuration references it.
    try { await saveKey(await modelCredentialAlias(draft), keyDraft.trim()) }
    catch { throw new Error('API Key 未能保存到 Windows 凭据管理器，模型配置没有改变。原有密钥未删除，请重试。') }
  }
  const matches = (saved: ProviderConfiguration) => (
    typeof saved.configuration_revision === 'string'
    && MODEL_CONFIGURATION_TOKEN.test(saved.configuration_revision) && saved.configuration_revision !== revision
    && (saved.credential_generation || '') === (generation || '')
    && saved.provider_id === draft.provider_id && saved.base_url === draft.base_url && saved.model === draft.model
    && saved.timeout_seconds === draft.timeout_seconds && saved.max_tokens === draft.max_tokens
    && saved.max_retries === draft.max_retries && saved.allow_tools === draft.allow_tools
    && saved.allow_streaming === draft.allow_streaming
  )
  try {
    const saved = await saveConfiguration(draft, revision)
    if (matches(saved)) return saved
  } catch (error) {
    if (error instanceof Error && 'status' in error) {
      if (error.status === 409) throw new Error('保存被拒绝：配置已变化，或仍有活动、排队或可恢复的任务。旧密钥未被覆盖或删除，请刷新设置后重试。')
      if (error.status === 400 || error.status === 422) throw new Error('保存被拒绝：模型配置无效。旧密钥未被覆盖或删除，请检查设置。')
    }
    // A lost reply is not a failed commit. Never reissue PUT or compensate by
    // deleting a slot which the in-flight request may already have selected.
  }
  if (staged && readConfiguration) {
    try {
      const observed = await readConfiguration()
      if (matches(observed)) return observed
    } catch { /* Outcome remains unknown; leave both slots untouched. */ }
  }
  throw new Error('未能确认保存结果。旧密钥未被覆盖或删除，未自动重试。请重新打开模型设置核对当前配置。')
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
