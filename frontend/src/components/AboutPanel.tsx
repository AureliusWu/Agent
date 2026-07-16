import { useState } from 'react'
import { AlertTriangle, Check, Clipboard, Info, Server, ShieldCheck } from 'lucide-react'
import type { BuildInfo } from '../buildInfo'
import type { BuildManifest } from '../buildInfoModel'
import { PanelHeader } from './PanelHeader'
import { BackupPanel } from './BackupPanel'
import { IdentityPanel } from './IdentityPanel'
import '../styles/panels.css'

interface Props { buildInfo: BuildInfo; apiOnline: boolean; apiAddress: string; workspace: string }

function displayTime(value: string) {
  return Number.isNaN(Date.parse(value)) ? value : new Date(value).toLocaleString()
}

function componentId(manifest: BuildManifest | null, component: keyof BuildManifest['component_build_ids']) {
  return manifest?.component_build_ids[component] || '尚未读取'
}

export function AboutPanel({ buildInfo, apiOnline, apiAddress, workspace }: Props) {
  const [copied, setCopied] = useState(false)
  const source = buildInfo.desktop || buildInfo.react
  const consistencyLabel = buildInfo.consistency.status === 'consistent'
    ? '构建一致'
    : buildInfo.consistency.status === 'mismatch' ? '构建不一致' : '构建信息读取中'

  async function copyDiagnostics() {
    const diagnostics = [
      '司忆构建信息',
      `产品版本号: ${source.product_version}`,
      `Git完整提交哈希: ${source.git_commit}`,
      `Git短提交哈希: ${source.git_short_commit}`,
      `Git分支: ${source.git_branch}`,
      `构建日期与时间: ${source.build_time}`,
      `构建类型: ${source.build_type}`,
      `构建时工作区状态: ${source.workspace_state}`,
      `源码内容指纹: ${source.source_fingerprint}`,
      `统一构建标识: ${source.build_id}`,
      `Tauri桌面端构建标识: ${componentId(buildInfo.desktop, 'tauri')}`,
      `React前端构建标识: ${componentId(buildInfo.react, 'react')}`,
      `Python Sidecar构建标识: ${componentId(buildInfo.sidecar, 'sidecar')}`,
      `数据库Schema版本: ${buildInfo.databaseSchemaVersion ?? source.database_schema_version}`,
      `三方状态: ${consistencyLabel}`,
      `桌面Build ID: ${buildInfo.desktop?.build_id || '尚未读取'}`,
      `前端Build ID: ${buildInfo.react.build_id}`,
      `Sidecar Build ID: ${buildInfo.sidecar?.build_id || '尚未读取'}`,
      `运行环境: ${buildInfo.environment}`,
      `司忆核心: ${apiOnline ? '已连接' : '未连接'}`,
      `API地址: ${apiAddress}`,
      `工作区: ${workspace || '未打开'}`,
    ].join('\n')
    await navigator.clipboard.writeText(diagnostics)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

  return <section className="content-panel">
    <PanelHeader icon={<Info />} title="关于司忆" subtitle="版本、构建指纹与统一运行诊断" />
    {buildInfo.consistency.status === 'mismatch' && <div className="build-warning" role="alert">
      <AlertTriangle size={18} />
      <div><strong>构建不一致</strong><span>桌面端、前端与 Sidecar 并非来自同一次构建，请重新完整构建后再提交问题。</span></div>
    </div>}
    <div className="about-grid">
      <article className="about-card build-details-card">
        <h3>完整构建信息</h3>
        <dl>
          <div><dt>产品版本号</dt><dd>v{source.product_version}</dd></div>
          <div><dt>Git 完整提交</dt><dd className="build-code">{source.git_commit}</dd></div>
          <div><dt>Git 短提交</dt><dd>{source.git_short_commit}</dd></div>
          <div><dt>Git 分支</dt><dd>{source.git_branch}</dd></div>
          <div><dt>构建日期与时间</dt><dd>{displayTime(source.build_time)}</dd></div>
          <div><dt>构建类型</dt><dd>{source.build_type}</dd></div>
          <div><dt>工作区状态</dt><dd>{source.workspace_state}</dd></div>
          <div><dt>源码内容指纹</dt><dd className="build-code">{source.source_fingerprint}</dd></div>
          <div><dt>统一构建标识</dt><dd className="build-code">{source.build_id}</dd></div>
          <div><dt>Tauri 桌面端</dt><dd className="build-code">{componentId(buildInfo.desktop, 'tauri')}</dd></div>
          <div><dt>React 前端</dt><dd className="build-code">{componentId(buildInfo.react, 'react')}</dd></div>
          <div><dt>Python Sidecar</dt><dd className="build-code">{componentId(buildInfo.sidecar, 'sidecar')}</dd></div>
          <div><dt>数据库 Schema</dt><dd>{buildInfo.databaseSchemaVersion ?? source.database_schema_version}</dd></div>
          <div><dt>三方状态</dt><dd className={buildInfo.consistency.status === 'mismatch' ? 'build-state-bad' : ''}>{consistencyLabel}</dd></div>
        </dl>
      </article>
      <article className="about-card">
        <h3>系统连接</h3>
        <dl>
          <div><dt>司忆核心</dt><dd>{apiOnline ? '已连接' : '未连接'}</dd></div>
          <div><dt>API 地址</dt><dd>{apiAddress}</dd></div>
          <div><dt>工作区</dt><dd>{workspace || '未打开'}</dd></div>
          <div><dt>敏感信息</dt><dd>不会复制 API Key</dd></div>
        </dl>
      </article>
      <IdentityPanel />
    </div>
    <div className="about-actions">
      <button className="primary" onClick={copyDiagnostics}>{copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? '已复制构建信息' : '复制构建信息'}</button>
      <span className="secondary"><Server size={15} />{apiOnline ? '司忆核心在线' : '司忆核心离线'}</span>
      <span className="secondary"><ShieldCheck size={15} />不包含密钥与令牌</span>
    </div>
    <BackupPanel />
  </section>
}
