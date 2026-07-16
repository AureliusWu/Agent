$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
function Clear-CoverageData {
    Get-ChildItem -LiteralPath $backend -Filter '.coverage*' -File -ErrorAction SilentlyContinue | Remove-Item -Force
}

Clear-CoverageData
& $python (Join-Path $root 'scripts\check-release-metadata.py')
if ($LASTEXITCODE -ne 0) {
    throw "Release metadata validation failed with exit code $LASTEXITCODE."
}
& $python (Join-Path $root 'scripts\generate_build_info.py') --build-type Development
if ($LASTEXITCODE -ne 0) {
    throw "Development build fingerprint generation failed with exit code $LASTEXITCODE."
}
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
