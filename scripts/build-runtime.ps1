param(
    [switch]$Force,
    # Candidate-only callers need the freshly built Tauri and sidecar outputs,
    # but must not overwrite the user's root portable runtime while collecting
    # isolated release evidence.
    [switch]$SkipPortableRuntimeSync,
    [string]$CargoTargetDirectory = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$backend = Join-Path $root 'siyi'
$desktop = Join-Path $root 'desktop'
$frontend = Join-Path $root 'desktop\frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $backend '.venv\Scripts\pyinstaller.exe'
$hookDirectory = Join-Path $root 'scripts\pyinstaller-hooks'
$tauri = Join-Path $frontend 'node_modules\.bin\tauri.cmd'
$binaryDirectory = Join-Path $root 'desktop\src-tauri\binaries'
$sidecarSource = Join-Path $binaryDirectory 'agent-backend-x86_64-pc-windows-msvc.exe'
$sidecarSupportDirectory = Join-Path $binaryDirectory '_internal'
$runtimeApplicationName = "{0}{1}.exe" -f [char]0x53F8, [char]0x5FC6
$runtimeApplication = Join-Path $root $runtimeApplicationName
$runtimeSidecar = Join-Path $root 'agent-backend.exe'
$runtimeSidecarSupportDirectory = Join-Path $root '_internal'
$sttHiddenImports = @(
    '--hidden-import', 'app.stt.worker',
    '--hidden-import', 'app.stt.providers.faster_whisper',
    '--hidden-import', 'faster_whisper',
    '--hidden-import', 'ctranslate2',
    '--exclude-module', 'av',
    '--hidden-import', 'onnxruntime',
    '--hidden-import', 'tokenizers',
    '--hidden-import', 'huggingface_hub'
)
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
    throw 'PyInstaller is missing from siyi/.venv. Install siyi/requirements.lock first.'
}
& $python (Join-Path $root 'scripts\check-python-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Release builds require Python 3.12.' }
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

function Sync-SidecarSupportDirectory([string]$Source, [string]$Destination, [string]$AllowedRoot) {
    $sourceFull = [System.IO.Path]::GetFullPath($Source)
    $destinationFull = [System.IO.Path]::GetFullPath($Destination)
    $allowedRootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not (Test-Path -LiteralPath $sourceFull -PathType Container)) {
        throw "The frozen sidecar support directory is missing: $sourceFull"
    }
    if (-not ($destinationFull + [System.IO.Path]::DirectorySeparatorChar).StartsWith($allowedRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to synchronize sidecar support outside its allowed directory: $destinationFull"
    }
    $linkedSource = Get-ChildItem -LiteralPath $sourceFull -Force -Recurse | Where-Object { $_.LinkType }
    if ($linkedSource) {
        throw "Refusing to package linked sidecar support entries from $sourceFull"
    }
    if (Test-Path -LiteralPath $destinationFull) {
        $existing = Get-Item -LiteralPath $destinationFull -Force
        if ($existing.LinkType) {
            throw "Refusing to replace linked sidecar support directory: $destinationFull"
        }
        Remove-Item -LiteralPath $destinationFull -Recurse -Force
    }
    Copy-Item -LiteralPath $sourceFull -Destination $destinationFull -Recurse -Force
}

$backendInputs = @(
    (Join-Path $root 'VERSION'),
    (Join-Path $backend 'app'),
    (Join-Path $backend 'run_server.py'),
    (Join-Path $backend 'pyproject.toml'),
    (Join-Path $backend 'requirements.lock'),
    (Join-Path $root 'scripts\check-python-runtime.py'),
    (Join-Path $root 'scripts\pyinstaller-hooks')
)
$sidecarSupportIsReady = (Test-Path -LiteralPath (Join-Path $sidecarSupportDirectory 'python312.dll') -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $sidecarSupportDirectory 'base_library.zip') -PathType Leaf)
$sidecarIsStale = $Force -or -not (Test-Path -LiteralPath $sidecarSource) -or -not $sidecarSupportIsReady
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
            & $pyinstaller --noconfirm --clean --onedir --name agent-backend `
                --add-data "${buildManifest};." `
                --additional-hooks-dir $hookDirectory `
                @sttHiddenImports `
                --workpath (Join-Path $temporaryRoot 'build') `
                --distpath (Join-Path $temporaryRoot 'dist') `
                --specpath (Join-Path $temporaryRoot 'spec') `
                run_server.py
            if ($LASTEXITCODE -ne 0) { throw "Sidecar build failed with exit code $LASTEXITCODE." }
        } finally {
            Pop-Location
        }
        New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null
        $builtSidecarDirectory = Join-Path $temporaryRoot 'dist\agent-backend'
        Copy-Item -LiteralPath (Join-Path $builtSidecarDirectory 'agent-backend.exe') -Destination $sidecarSource -Force
        Sync-SidecarSupportDirectory (Join-Path $builtSidecarDirectory '_internal') $sidecarSupportDirectory $binaryDirectory
    } finally {
        if (Test-Path -LiteralPath $temporaryRoot) {
            Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
        }
    }
}

& $python (Join-Path $root 'scripts\check-release-metadata.py')
if ($LASTEXITCODE -ne 0) { throw 'Release metadata validation failed.' }

. (Join-Path $PSScriptRoot 'Import-MsvcEnvironment.ps1')
New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
$env:CARGO_TARGET_DIR = $targetDirectory

Push-Location $desktop
try {
    if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules'))) {
        npm.cmd --prefix frontend ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed with exit code $LASTEXITCODE." }
    }
    $frontendDist = Join-Path $frontend 'dist'
    if (Test-Path -LiteralPath $frontendDist) {
        Remove-Item -LiteralPath $frontendDist -Recurse -Force
    }
    cargo clean --manifest-path (Join-Path $root 'desktop\src-tauri\Cargo.toml') -p app
    if ($LASTEXITCODE -ne 0) { throw "Desktop application cache cleanup failed with exit code $LASTEXITCODE." }
    & $tauri build --config src-tauri\tauri.conf.json --no-bundle --ci
    if ($LASTEXITCODE -ne 0) { throw "Desktop runtime build failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}

$releaseDirectory = Join-Path $targetDirectory 'release'
$builtApplication = Join-Path $releaseDirectory $runtimeApplicationName
$builtSidecar = Join-Path $releaseDirectory 'agent-backend.exe'
if (-not (Test-Path -LiteralPath $builtApplication) -or -not (Test-Path -LiteralPath $builtSidecar) -or -not (Test-Path -LiteralPath (Join-Path $releaseDirectory '_internal') -PathType Container)) {
    throw "Desktop build output is incomplete under $releaseDirectory."
}

$expectedVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$builtVersion = (Get-Item -LiteralPath $builtApplication).VersionInfo.ProductVersion
if (-not $builtVersion.StartsWith($expectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Built runtime version mismatch: expected $expectedVersion, found $builtVersion."
}
if ($SkipPortableRuntimeSync) {
    Write-Host "Candidate runtime ready without replacing the root portable runtime: $builtApplication ($builtVersion)" -ForegroundColor Green
    return
}

$runtimeApplicationFull = [System.IO.Path]::GetFullPath($runtimeApplication)
$rootBoundary = $root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if (-not $runtimeApplicationFull.StartsWith($rootBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to write runtime outside repository: $runtimeApplicationFull"
}
Copy-Item -LiteralPath $builtApplication -Destination $runtimeApplication -Force
Copy-Item -LiteralPath $builtSidecar -Destination $runtimeSidecar -Force
Sync-SidecarSupportDirectory (Join-Path $releaseDirectory '_internal') $runtimeSidecarSupportDirectory $root

$copiedVersion = (Get-Item -LiteralPath $runtimeApplication).VersionInfo.ProductVersion
if (-not $copiedVersion.StartsWith($expectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Runtime version mismatch: expected $expectedVersion, found $copiedVersion."
}

Write-Host "Runtime ready: $runtimeApplication ($copiedVersion)" -ForegroundColor Green
