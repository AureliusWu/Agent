$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
function Clear-CoverageData {
    Get-ChildItem -LiteralPath $backend -Filter '.coverage*' -File -ErrorAction SilentlyContinue | Remove-Item -Force
}

Clear-CoverageData
Push-Location $backend
try {
    .\.venv\Scripts\python -m pytest -q
} finally {
    Pop-Location
    Clear-CoverageData
}
Push-Location (Join-Path $root 'frontend')
try { npm run lint; npm run build } finally { Pop-Location }
