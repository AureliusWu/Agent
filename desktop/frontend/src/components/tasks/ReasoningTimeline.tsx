import { Brain, ChevronDown } from 'lucide-react'
import ReactMarkdown from 'react-markdown'

export function ReasoningTimeline({ content }: { content: string }) {
  return <details className="reasoning-timeline">
    <summary><Brain size={14} /><span>DeepSeek 原生推理</span><small>模型返回</small><ChevronDown size={14} /></summary>
    <div className="reasoning-timeline-body"><ReactMarkdown>{content}</ReactMarkdown></div>
  </details>
}
