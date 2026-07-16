param(
    [switch]$Force,
    [string]$CargoTargetDirectory = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $backend '.venv\Scripts\pyinstaller.exe'
$binaryDirectory = Join-Path $frontend 'src-tauri\binaries'
$sidecarSource = Join-Path $binaryDirectory 'agent-backend-x86_64-pc-windows-msvc.exe'
$runtimeApplicationName = "{0}{1}.exe" -f [char]0x53F8, [char]0x5FC6
$runtimeApplication = Join-Path $root $runtimeApplicationName
$runtimeSidecar = Join-Path $root 'agent-backend.exe'
$cacheRoot = Join-Path $env:LOCALAPPDATA 'AureliusWu\AgentBuildCache'
$targetDirectory = if ($CargoTargetDirectory) {
    [System.IO.Path]::GetFullPath($CargoTargetDirectory)
} else {
    Join-Path $cacheRoot 'target'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.'
}
if (-not (Test-Path -LiteralPath $pyinstaller)) {
    throw 'PyInstaller is missing from backend/.venv. Install backend/requirements.lock first.'
}
$buildManifest = Join-Path $root 'build\generated\build-info.json'
$env:SIYI_BUILD_MANIFEST = $buildManifest
$env:SIYI_BUILD_INFO_LOCKED = '1'
& $python (Join-Path $root 'scripts\generate_build_info.py') --output $buildManifest --build-type Release
if ($LASTEXITCODE -ne 0) { throw 'Build manifest generation failed.' }

function Get-LatestWriteTime([string[]]$Paths) {
    $files = foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -PathType Container) {
            Get-ChildItem -LiteralPath $path -File -Recurse -Force
        } elseif (Test-Path -LiteralPath $path -PathType Leaf) {
            Get-Item -LiteralPath $path
        }
    }
    return ($files | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).LastWriteTimeUtc
}

$backendInputs = @(
    (Join-Path $backend 'app'),
    (Join-Path $backend 'run_server.py'),
    (Join-Path $backend 'pyproject.toml'),
    (Join-Path $backend 'requirements.lock')
)
$sidecarIsStale = $Force -or -not (Test-Path -LiteralPath $sidecarSource)
if (-not $sidecarIsStale) {
    $sidecarIsStale = (Get-LatestWriteTime $backendInputs) -gt (Get-Item -LiteralPath $sidecarSource).LastWriteTimeUtc
}

if ($sidecarIsStale) {
    Write-Host 'Building the local FastAPI sidecar...' -ForegroundColor Cyan
    $temporaryRoot = [System.IO.Path]::GetFullPath((Join-Path $root (".runtime-build-" + [Guid]::NewGuid().ToString('N'))))
    $rootBoundary = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not ($temporaryRoot + [System.IO.Path]::DirectorySeparatorChar).StartsWith($rootBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to build outside repository: $temporaryRoot"
    }
    New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
    try {
        Push-Location $backend
        try {
            & $pyinstaller --noconfirm --clean --onefile --name agent-backend `
                --add-data "${buildManifest};." `
                --workpath (Join-Path $temporaryRoot 'build') `
                --distpath (Join-Path $temporaryRoot 'dist') `
                --specpath (Join-Path $temporaryRoot 'spec') `
                run_server.py
            if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed with exit code $LASTEXITCODE." }
        } finally {
            Pop-Location
        }
        New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null
        Copy-Item -LiteralPath (Join-Path $temporaryRoot 'dist\agent-backend.exe') -Destination $sidecarSource -Force
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }
}

& $python (Join-Path $root 'scripts\check-release-metadata.py')
if ($LASTEXITCODE -ne 0) { throw 'Release metadata validation failed.' }

. (Join-Path $PSScriptRoot 'Import-MsvcEnvironment.ps1')
New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
$env:CARGO_TARGET_DIR = $targetDirectory

Push-Location $frontend
try {
    if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed with exit code $LASTEXITCODE." }
    }
    $frontendDist = Join-Path $frontend 'dist'
    if (Test-Path -LiteralPath $frontendDist) {
        Remove-Item -LiteralPath $frontendDist -Recurse -Force
    }
    cargo clean --manifest-path (Join-Path $frontend 'src-tauri\Cargo.toml') -p app
    if ($LASTEXITCODE -ne 0) { throw "Desktop application cache cleanup failed with exit code $LASTEXITCODE." }
    npm.cmd run tauri -- build --no-bundle --ci
    if ($LASTEXITCODE -ne 0) { throw "Desktop runtime build failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

$releaseDirectory = Join-Path $targetDirectory 'release'
$builtApplication = Join-Path $releaseDirectory $runtimeApplicationName
$builtSidecar = Join-Path $releaseDirectory 'agent-backend.exe'
if (-not (Test-Path -LiteralPath $builtApplication) -or -not (Test-Path -LiteralPath $builtSidecar)) {
    throw "Desktop build output is incomplete under $releaseDirectory."
}

$runtimeApplicationFull = [System.IO.Path]::GetFullPath($runtimeApplication)
$rootBoundary = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $runtimeApplicationFull.StartsWith($rootBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write runtime outside repository: $runtimeApplicationFull"
}
Copy-Item -LiteralPath $builtApplication -Destination $runtimeApplication -Force
Copy-Item -LiteralPath $builtSidecar -Destination $runtimeSidecar -Force

$expectedVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$copiedVersion = (Get-Item -LiteralPath $runtimeApplication).VersionInfo.ProductVersion
if (-not $copiedVersion.StartsWith($expectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Runtime version mismatch: expected $expectedVersion, found $copiedVersion."
}

Write-Host "Runtime ready: $runtimeApplication ($copiedVersion)" -ForegroundColor Green
