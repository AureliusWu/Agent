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
    $backendExitCode = $LASTEXITCODE
} finally {
    Pop-Location
    Clear-CoverageData
}
if ($backendExitCode -ne 0) {
    throw "Backend tests failed with exit code $backendExitCode."
}
Push-Location (Join-Path $root 'frontend')
try {
    npm run lint
    $lintExitCode = $LASTEXITCODE
    if ($lintExitCode -ne 0) {
        throw "Frontend lint failed with exit code $lintExitCode."
    }
    npm run build
    $buildExitCode = $LASTEXITCODE
    if ($buildExitCode -ne 0) {
        throw "Frontend build failed with exit code $buildExitCode."
    }
    npm run test:security
    $securityExitCode = $LASTEXITCODE
    if ($securityExitCode -ne 0) {
        throw "Frontend security check failed with exit code $securityExitCode."
    }
} finally {
    Pop-Location
}
