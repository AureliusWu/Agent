param(
    [string]$PreviousInstaller = '',
    [string]$PerformanceBaselineBinary = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$desktop = Join-Path $root 'desktop'
$frontend = Join-Path $root 'desktop\frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $backend '.venv\Scripts\pyinstaller.exe'
$tauri = Join-Path $frontend 'node_modules\.bin\tauri.cmd'
$buildDirectory = Join-Path $root 'build\pyinstaller'
$distDirectory = Join-Path $root 'dist\sidecar'
$hookDirectory = Join-Path $root 'scripts\pyinstaller-hooks'
$binaryDirectory = Join-Path $root 'desktop\src-tauri\binaries'
$target = Join-Path $binaryDirectory 'agent-backend-x86_64-pc-windows-msvc.exe'

if (-not (Test-Path $python)) { throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.' }
& $python (Join-Path $root 'scripts\check-python-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Release builds require Python 3.12.' }
$buildManifest = Join-Path $root 'build\generated\build-info.json'
$env:SIYI_BUILD_MANIFEST = $buildManifest
$env:SIYI_BUILD_INFO_LOCKED = '1'
& $python (Join-Path $root 'scripts\generate_build_info.py') --output $buildManifest --build-type Release
if ($LASTEXITCODE -ne 0) { throw 'Build manifest generation failed.' }
& $python (Join-Path $root 'scripts\check-release-metadata.py')
if ($LASTEXITCODE -ne 0) { throw 'Release metadata validation failed.' }
& $python -m pip install -r (Join-Path $backend 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed with exit code $LASTEXITCODE." }
& $python -m pip install --no-deps -e $backend
if ($LASTEXITCODE -ne 0) { throw "Backend package installation failed with exit code $LASTEXITCODE." }
Push-Location $backend
try {
    & $pyinstaller --noconfirm --clean --onefile --name agent-backend --add-data "${buildManifest};." --additional-hooks-dir $hookDirectory --workpath $buildDirectory --distpath $distDirectory --specpath (Join-Path $root 'build') run_server.py
    $sidecarExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($sidecarExitCode -ne 0) { throw "Sidecar build failed with exit code $sidecarExitCode." }
New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $distDirectory 'agent-backend.exe') -Destination $target -Force
& $python (Join-Path $root 'scripts\check-frozen-artifacts.py') `
    --binary $target `
    --output (Join-Path $root 'build\v970-evidence\frozen-artifacts.json')
if ($LASTEXITCODE -ne 0) { throw 'Frozen Artifact Engine inventory gate failed.' }

. (Join-Path $PSScriptRoot 'Import-MsvcEnvironment.ps1')
Push-Location $desktop
try {
    if (-not (Test-Path -LiteralPath $tauri)) {
        npm.cmd --prefix frontend ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed with exit code $LASTEXITCODE." }
    }
    cargo metadata --locked --manifest-path src-tauri\Cargo.toml --format-version 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cargo lock validation failed with exit code $LASTEXITCODE." }
    & $tauri build --config src-tauri\tauri.conf.json
    $desktopExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($desktopExitCode -ne 0) { throw "Desktop packaging failed with exit code $desktopExitCode." }
$runtimeApplicationName = "{0}{1}.exe" -f [char]0x53F8, [char]0x5FC6
$builtApplication = Join-Path $root "desktop\src-tauri\target\release\$runtimeApplicationName"
$runtimeApplication = Join-Path $root $runtimeApplicationName
$runtimeSidecar = Join-Path $root 'agent-backend.exe'
if (-not (Test-Path -LiteralPath $builtApplication)) {
    throw "Desktop build output is missing: $builtApplication"
}
Copy-Item -LiteralPath $builtApplication -Destination $runtimeApplication -Force
Copy-Item -LiteralPath $target -Destination $runtimeSidecar -Force
$expectedVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$copiedVersion = (Get-Item -LiteralPath $runtimeApplication).VersionInfo.ProductVersion
if (-not $copiedVersion.StartsWith($expectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Runtime version mismatch: expected $expectedVersion, found $copiedVersion."
}
& (Join-Path $PSScriptRoot 'smoke-sidecar.ps1') -Binary $target -ArtifactSmoke
& (Join-Path $PSScriptRoot 'smoke-installer.ps1') -PreviousInstaller $PreviousInstaller
if (-not $PreviousInstaller) {
    throw 'The previous NSIS installer is required for upgrade and package-size validation.'
}
$candidateInstaller = Get-ChildItem -LiteralPath (Join-Path $desktop 'src-tauri\target\release\bundle\nsis') `
    -Filter '*setup.exe' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (-not $candidateInstaller) { throw 'Candidate NSIS installer was not produced.' }
& $python (Join-Path $root 'scripts\check-package-size.py') `
    --candidate $candidateInstaller.FullName `
    --baseline $PreviousInstaller `
    --output (Join-Path $root 'build\v970-evidence\package-size.json')
if ($LASTEXITCODE -ne 0) { throw 'NSIS package-size gate failed.' }
if (-not $PerformanceBaselineBinary) {
    throw 'A clean previous-release sidecar is required for paired performance validation.'
}
& $python (Join-Path $root 'scripts\run-v8-performance-gate.py') `
    --baseline-binary $PerformanceBaselineBinary `
    --require-paired-baseline `
    --output (Join-Path $root 'build\v970-evidence\performance-gate.json')
if ($LASTEXITCODE -ne 0) { throw 'Paired performance gate failed.' }
& $python (Join-Path $root 'scripts\generate-sbom.py')
if ($LASTEXITCODE -ne 0) { throw 'SBOM generation failed.' }
& $python (Join-Path $root 'scripts\generate-third-party-notices.py')
if ($LASTEXITCODE -ne 0) { throw 'Third-party notice generation failed.' }
