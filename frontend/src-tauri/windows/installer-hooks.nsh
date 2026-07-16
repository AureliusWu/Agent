!macro NSIS_HOOK_PREINSTALL
  ; v1.x used the product name Agent. Uninstall it before installing the renamed
  ; desktop app so the old executable, shortcuts and uninstall entry do not remain.
  ReadRegStr $R8 SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\Agent" "UninstallString"
  StrCmp $R8 "" legacy_agent_not_installed
    ClearErrors
    ExecWait '$R8 /S' $R9
legacy_agent_not_installed:
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; Also clean a portable/same-directory v1.x copy that has no uninstall entry.
  Delete "$INSTDIR\Agent.exe"
  Delete "$DESKTOP\Agent.lnk"
  Delete "$SMPROGRAMS\Agent.lnk"
  DeleteRegKey SHCTX "Software\Microsoft\Windows\CurrentVersion\Uninstall\Agent"
  DeleteRegKey SHCTX "Software\github\Agent"
!macroend
