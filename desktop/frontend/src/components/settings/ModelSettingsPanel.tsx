import { DeepSeekProviderPanel } from '../providers/DeepSeekProviderPanel'

export function ModelSettingsPanel() {
  return <section className="content-panel model-settings-page" aria-label="模型与 API 设置">
    <DeepSeekProviderPanel />
  </section>
}
