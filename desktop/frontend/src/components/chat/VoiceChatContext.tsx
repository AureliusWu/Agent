/* oxlint-disable react/only-export-components */
import { createContext, useContext } from 'react'
import type { ReactNode } from 'react'
import type { VoiceTranscriptInput } from '../../hooks/useVoiceCapture'

interface VoiceChatBridge {
  conversationId: number | null
  boundVoiceSessionId: string | null
  onInterruptTts: () => Promise<void> | void
  onTranscript: (input: VoiceTranscriptInput) => Promise<void> | void
  onDiscardTranscript: (voiceSessionId: string) => void
}

const VoiceChatContext = createContext<VoiceChatBridge | null>(null)

export function VoiceChatProvider({ value, children }: { value: VoiceChatBridge; children: ReactNode }) {
  return <VoiceChatContext.Provider value={value}>{children}</VoiceChatContext.Provider>
}

export function useVoiceChat() {
  return useContext(VoiceChatContext)
}
