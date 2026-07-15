import { useState } from 'react'
import { Check, Clipboard, Info, Server, ShieldCheck } from 'lucide-react'
import type { BuildInfo } from '../buildInfo'
import { PanelHeader } from './PanelHeader'
import '../styles/panels.css'

interface Props {
  buildInfo: BuildInfo
  apiOnline: boolean
  apiAddress: string
  workspace: string
}

export function AboutPanel({ buildInfo, apiOnline, apiAddress, workspace }: Props) {
  const [copied, setCopied] = useState(false)
  const formattedTime = Number.isNaN(Date.parse(buildInfo.buildTime)) ? buildInfo.buildTime : new Date(buildInfo.buildTime).toLocaleString()

  async function copyDiagnostics() {
    const diagnostics = [
      `Agent: ${buildInfo.version}`,
      `Runtime: ${buildInfo.environment}`,
      `Git commit: ${buildInfo.commit}`,
      `Build time: ${buildInfo.buildTime}`,
      `Siyi core: ${apiOnline ? 'connected' : 'disconnected'}`,
      `API address: ${apiAddress}`,
      `Workspace: ${workspace}`,
    ].join('\n')
    await navigator.clipboard.writeText(diagnostics)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1800)
  }

  return <section className="content-panel">
    <PanelHeader icon={<Info />} title="关于记忆海终端" subtitle="版本、运行环境与无敏感信息的诊断数据" />
    <div className="about-grid">
      <article className="about-card">
        <h3>应用构建</h3>
        <dl>
          <div><dt>应用版本</dt><dd>v{buildInfo.version}</dd></div>
          <div><dt>运行环境</dt><dd>{buildInfo.environment}</dd></div>
          <div><dt>Git 提交</dt><dd>{buildInfo.commit}</dd></div>
          <div><dt>构建时间</dt><dd>{formattedTime}</dd></div>
        </dl>
      </article>
      <article className="about-card">
        <h3>系统连接</h3>
        <dl>
          <div><dt>司忆核心</dt><dd>{apiOnline ? '已连接' : '未连接'}</dd></div>
          <div><dt>API 地址</dt><dd>{apiAddress}</dd></div>
          <div><dt>工作区</dt><dd>{workspace}</dd></div>
          <div><dt>敏感信息</dt><dd>不会复制 API Key</dd></div>
        </dl>
      </article>
    </div>
    <div className="about-actions">
      <button className="primary" onClick={copyDiagnostics}>{copied ? <Check size={15} /> : <Clipboard size={15} />}{copied ? '已复制诊断信息' : '复制诊断信息'}</button>
      <span className="secondary"><Server size={15} />{apiOnline ? '司忆核心在线' : '司忆核心离线'}</span>
      <span className="secondary"><ShieldCheck size={15} />不包含密钥与令牌</span>
    </div>
  </section>
}
