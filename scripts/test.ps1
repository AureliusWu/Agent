$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $root 'backend')
try { .\.venv\Scripts\python -m pytest -q } finally { Pop-Location }
Push-Location (Join-Path $root 'frontend')
try { npm run lint; npm run build } finally { Pop-Location }
