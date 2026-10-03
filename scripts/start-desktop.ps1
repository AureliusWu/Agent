param([string]$ApplicationPath)
$ErrorActionPreference = 'Stop'
$portableName = ([string][char]0x53f8) + [char]0x5fc6 + '.exe'
$application = if ($ApplicationPath) { $ApplicationPath } else { Join-Path (Split-Path -Parent $PSScriptRoot) $portableName }
if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
  throw 'Portable application is missing. Build the runtime first.'
}
# Explorer/Codex may predate a local data migration. Read persisted local paths
# at launch, while preserving explicit test-owned process overrides.
foreach ($name in @('AGENT_DESKTOP_DATA_DIRECTORY', 'AGENT_DATA_ROOT', 'AGENT_ENV_FILE')) {
  if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
    $configured = [Environment]::GetEnvironmentVariable($name, 'User')
    if ($configured) { [Environment]::SetEnvironmentVariable($name, $configured, 'Process') }
  }
}
Start-Process -FilePath $application -WorkingDirectory (Split-Path -Parent $application)
