import type { ReactNode } from 'react'
import { CharacterPortrait } from './CharacterPortrait'

export interface CharacterCardProps {
  name: string
  subtitle?: string
  imageSrc: string
  status?: string
  description?: string
  actions?: ReactNode
}

export function CharacterCard({ name, subtitle, imageSrc, status, description, actions }: CharacterCardProps) {
  return <section className="character-card">
    <header>
      <span className={status === 'busy' ? 'real-status busy' : 'real-status'} title={status === 'busy' ? '正在执行' : '待命'} aria-label={status === 'busy' ? '正在执行' : '待命'} />
      <div className="character-title">
        <h2>{name}</h2>
        {subtitle && <p>{subtitle}</p>}
      </div>
      {actions}
    </header>
    <CharacterPortrait imageSrc={imageSrc} name={name} />
    {description && <p className="character-description">{description}</p>}
  </section>
}
