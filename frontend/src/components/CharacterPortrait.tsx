import { useEffect, useState } from 'react'
import { ImageOff } from 'lucide-react'

interface Props {
  imageSrc: string
  name: string
}

export function CharacterPortrait({ imageSrc, name }: Props) {
  const [failed, setFailed] = useState(false)

  useEffect(() => setFailed(false), [imageSrc])

  const fail = () => {
    setFailed(true)
    if (import.meta.env.DEV) console.error(`Character portrait failed to load: ${imageSrc}`)
  }

  return <div className="character-portrait">
    {failed
      ? <div className="character-portrait-fallback" role="img" aria-label={`${name}角色图暂不可用`}><ImageOff size={24} /><span>角色图暂不可用</span></div>
      : <img src={imageSrc} alt={name} onError={fail} />}
  </div>
}
