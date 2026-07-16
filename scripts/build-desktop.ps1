param(
    [string]$PreviousInstaller = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $backend '.venv\Scripts\pyinstaller.exe'
$buildDirectory = Join-Path $root 'build\pyinstaller'
$distDirectory = Join-Path $root 'dist\sidecar'
$binaryDirectory = Join-Path $frontend 'src-tauri\binaries'
$target = Join-Path $binaryDirectory 'agent-backend-x86_64-pc-windows-msvc.exe'

if (-not (Test-Path $python)) { throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.' }
& $python (Join-Path $root 'scripts\check-release-metadata.py')
if ($LASTEXITCODE -ne 0) { throw 'Release metadata validation failed.' }
& $python -m pip install -r (Join-Path $backend 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed with exit code $LASTEXITCODE." }
& $python -m pip install --no-deps -e $backend
if ($LASTEXITCODE -ne 0) { throw "Backend package installation failed with exit code $LASTEXITCODE." }
Push-Location $backend
try {
    & $pyinstaller --noconfirm --clean --onefile --name agent-backend --workpath $buildDirectory --distpath $distDirectory --specpath (Join-Path $root 'build') run_server.py
    $sidecarExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($sidecarExitCode -ne 0) { throw "Sidecar build failed with exit code $sidecarExitCode." }
New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $distDirectory 'agent-backend.exe') -Destination $target -Force

. (Join-Path $PSScriptRoot 'Import-MsvcEnvironment.ps1')
Push-Location $frontend
try {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed with exit code $LASTEXITCODE." }
    cargo metadata --locked --manifest-path src-tauri\Cargo.toml --format-version 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cargo lock validation failed with exit code $LASTEXITCODE." }
    npm run tauri build
    $desktopExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($desktopExitCode -ne 0) { throw "Desktop packaging failed with exit code $desktopExitCode." }
& (Join-Path $PSScriptRoot 'smoke-sidecar.ps1') -Binary $target
& (Join-Path $PSScriptRoot 'smoke-installer.ps1') -PreviousInstaller $PreviousInstaller
& $python (Join-Path $root 'scripts\generate-sbom.py')
