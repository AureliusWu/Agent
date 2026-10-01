param(
    [string]$PreviousInstaller = '',
    [string]$PreviousNsisInstaller = '',
    [string]$PreviousMsiInstaller = '',
    [string]$PerformanceBaselineBinary = '',
    [switch]$AllowUnpairedPerformanceBaseline,
    [switch]$ReleaseEvidence,
    [string]$EvidenceVersion = '',
    [switch]$CandidateOnly,
    [string]$CandidateDirectory = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$evidenceArguments = @{ RepositoryRoot = $root }
if ($EvidenceVersion) { $evidenceArguments.EvidenceVersion = $EvidenceVersion }
$evidenceRoot = & (Join-Path $PSScriptRoot 'evidence-root.ps1') @evidenceArguments
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
$targetSupportDirectory = Join-Path $binaryDirectory '_internal'
$trackedSupportPlaceholderRelativePath = 'desktop/src-tauri/binaries/_internal/.gitkeep'
$desktopTargetDirectory = Join-Path $desktop 'src-tauri\target'
$specDirectory = Join-Path $root 'build'
$previousNsis = if ($PreviousNsisInstaller) { $PreviousNsisInstaller } else { $PreviousInstaller }
if ($PreviousInstaller -and $PreviousNsisInstaller) {
    $legacyPrevious = (Resolve-Path -LiteralPath $PreviousInstaller).Path
    $explicitPrevious = (Resolve-Path -LiteralPath $PreviousNsisInstaller).Path
    if (-not $legacyPrevious.Equals($explicitPrevious, [StringComparison]::OrdinalIgnoreCase)) {
        throw '-PreviousInstaller and -PreviousNsisInstaller must identify the same file when both are supplied.'
    }
}
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

function Read-HeadBlobBytes([string]$RepositoryRoot, [string]$RelativePath) {
    $revision = "HEAD:$RelativePath"
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'git.exe'
    $startInfo.Arguments = ('-C "{0}" show "{1}"' -f $RepositoryRoot, $revision)
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.UseShellExecute = $false
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Unable to read tracked placeholder from $revision"
    }
    $output = [System.IO.MemoryStream]::new()
    try {
        $process.StandardOutput.BaseStream.CopyTo($output)
        $errorOutput = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Unable to read tracked placeholder from ${revision}: $errorOutput"
        }
        return ,$output.ToArray()
    } finally {
        $output.Dispose()
        $process.Dispose()
    }
}

function Restore-TrackedSidecarSupportPlaceholder(
    [string]$RepositoryRoot,
    [string]$SupportDirectory,
    [string]$AllowedRoot,
    [string]$RelativePath
) {
    $supportDirectoryFull = [System.IO.Path]::GetFullPath($SupportDirectory)
    $allowedRootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not ($supportDirectoryFull + [System.IO.Path]::DirectorySeparatorChar).StartsWith($allowedRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to restore tracked placeholder outside its allowed directory: $supportDirectoryFull"
    }
    $headBytes = Read-HeadBlobBytes $RepositoryRoot $RelativePath
    if ($headBytes.Length -ne 1) {
        throw "Tracked placeholder must remain a single byte in HEAD: $RelativePath"
    }
    if (Test-Path -LiteralPath $supportDirectoryFull) {
        $supportDirectoryItem = Get-Item -LiteralPath $supportDirectoryFull -Force
        if (-not $supportDirectoryItem.PSIsContainer -or $supportDirectoryItem.LinkType) {
            throw "Refusing to restore tracked placeholder into linked or non-directory support path: $supportDirectoryFull"
        }
    } else {
        New-Item -ItemType Directory -Force -Path $supportDirectoryFull | Out-Null
    }
    $placeholder = Join-Path $supportDirectoryFull '.gitkeep'
    if (Test-Path -LiteralPath $placeholder) {
        $placeholderItem = Get-Item -LiteralPath $placeholder -Force
        if ($placeholderItem.PSIsContainer -or $placeholderItem.LinkType) {
            throw "Refusing to restore tracked placeholder over linked or non-file path: $placeholder"
        }
    }
    [System.IO.File]::WriteAllBytes($placeholder, $headBytes)
    $restoredBytes = [System.IO.File]::ReadAllBytes($placeholder)
    if ($restoredBytes.Length -ne 1 -or $restoredBytes[0] -ne $headBytes[0]) {
        throw "Tracked placeholder restoration did not match HEAD: $RelativePath"
    }
}

function Resolve-CandidateDirectory([string]$RepositoryRoot, [string]$RequestedPath, [string]$Version, [switch]$AllowExisting) {
    $repositoryFull = [System.IO.Path]::GetFullPath($RepositoryRoot)
    $allowed = [System.IO.Path]::GetFullPath((Join-Path $repositoryFull 'build\candidates'))
    $prefix = $allowed.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') { throw 'Candidate version is invalid.' }
    $requested = if ($RequestedPath) { $RequestedPath } else {
        Join-Path $allowed ("v{0}-{1}-{2}" -f $Version, (Get-Date -Format 'yyyyMMdd-HHmmss'), [guid]::NewGuid().ToString('N').Substring(0, 8))
    }
    if (-not [System.IO.Path]::IsPathRooted($requested)) { $requested = Join-Path $repositoryFull $requested }
    $resolved = [System.IO.Path]::GetFullPath($requested)
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Candidate output must be a new directory below build\candidates.'
    }
    $cursor = [System.IO.DirectoryInfo]::new($resolved)
    while ($cursor -and $cursor.FullName.StartsWith($repositoryFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $cursor.FullName) {
            $item = Get-Item -LiteralPath $cursor.FullName -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw 'Candidate output ancestors must be ordinary directories without reparse points.'
            }
        }
        if ($cursor.FullName.Equals($repositoryFull, [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = $cursor.Parent
    }
    if (-not $AllowExisting -and (Test-Path -LiteralPath $resolved)) {
        throw 'Candidate output already exists; choose a fresh directory to retain prior results.'
    }
    return $resolved
}

function Test-ExistingCandidateDependencies([string]$Python, [string]$LockFile, [string]$BackendDirectory) {
    # Read metadata only. This branch never installs or upgrades dependencies.
    $dependencyCheck = @'
import importlib.metadata as metadata
from pathlib import Path
import sys
from packaging.requirements import Requirement

failures = []
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    value = line.strip()
    if not value or value.startswith("#"):
        continue
    requirement = Requirement(value)
    if requirement.marker and not requirement.marker.evaluate():
        continue
    try:
        actual = metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        failures.append(requirement.name + " is missing")
        continue
    if actual not in requirement.specifier:
        failures.append(requirement.name + " differs from requirements.lock")
sys.path.insert(0, sys.argv[2])
import app
if failures:
    raise SystemExit("Candidate dependencies are not ready: " + "; ".join(failures))
print("Existing locked Python dependencies and backend import: PASS")
'@
    & $Python -c $dependencyCheck $LockFile $BackendDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Candidate dependency verification failed; no packages were installed.' }
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Existing dependency compatibility check failed; no packages were installed.' }
}

$candidateRoot = $null
if ($CandidateOnly) {
    if ($ReleaseEvidence -or $PreviousInstaller -or $PreviousNsisInstaller -or $PreviousMsiInstaller -or $PerformanceBaselineBinary -or $AllowUnpairedPerformanceBaseline) {
        throw 'CandidateOnly cannot be combined with installation, upgrade, performance or formal release options.'
    }
    $candidateVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw -Encoding ascii).Trim()
    if ($EvidenceVersion -and (($EvidenceVersion.Trim() -replace '^v(?=\d)', '') -ne $candidateVersion)) {
        throw 'Candidate evidence version must match VERSION.'
    }
    $candidateRoot = Resolve-CandidateDirectory $root $CandidateDirectory $candidateVersion
    if (-not (Test-Path -LiteralPath $pyinstaller -PathType Leaf) -or -not (Test-Path -LiteralPath $tauri -PathType Leaf)) {
        throw 'CandidateOnly needs existing PyInstaller and frontend dependencies; no installation is performed.'
    }
    $buildDirectory = Join-Path $candidateRoot 'pyinstaller-work'
    $distDirectory = Join-Path $candidateRoot 'sidecar-dist'
    $specDirectory = Join-Path $candidateRoot 'pyinstaller-spec'
    $evidenceRoot = Join-Path $candidateRoot 'evidence'
} elseif ($CandidateDirectory) {
    throw 'CandidateDirectory requires CandidateOnly.'
}

$previousCandidateEnvironment = @{}
if ($CandidateOnly) {
    foreach ($name in @('SIYI_BUILD_MANIFEST', 'SIYI_BUILD_INFO_LOCKED', 'SIYI_CANDIDATE_FRONTEND_DIST', 'CARGO_TARGET_DIR', 'CARGO_NET_OFFLINE', 'CARGO_BUILD_JOBS')) {
        $previousCandidateEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
}

try {

if (-not (Test-Path $python)) { throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.' }
& $python (Join-Path $root 'scripts\check-python-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Release builds require Python 3.12.' }
if ($CandidateOnly) {
    Test-ExistingCandidateDependencies $python (Join-Path $backend 'requirements.lock') $backend
    New-Item -ItemType Directory -Path $candidateRoot | Out-Null
    $env:CARGO_TARGET_DIR = $desktopTargetDirectory
    $env:CARGO_NET_OFFLINE = 'true'
    $env:CARGO_BUILD_JOBS = '1'
    $candidateFrontendDist = Join-Path $candidateRoot 'frontend-dist'
    $env:SIYI_CANDIDATE_FRONTEND_DIST = $candidateFrontendDist
    # FrontendDist is an untagged URL-first enum: a Windows drive path is
    # parsed as a drive-scheme URL, so it does not embed assets. Keep this path relative
    # to src-tauri while Vite receives the validated absolute output separately.
    $candidateFrontendDistRelative = '../../' + $candidateRoot.Substring($root.Length + 1).Replace('\', '/') + '/frontend-dist'
    $candidateTauriConfig = Join-Path $candidateRoot 'tauri-candidate.json'
    $candidateConfigJson = @{
        build = @{
            frontendDist = $candidateFrontendDistRelative
            beforeBuildCommand = 'npm --prefix ../frontend run build:desktop'
        }
    } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($candidateTauriConfig, $candidateConfigJson, [System.Text.UTF8Encoding]::new($false))
}
$buildManifest = if ($CandidateOnly) { Join-Path $candidateRoot 'build-info.json' } else { Join-Path $root 'build\generated\build-info.json' }
$env:SIYI_BUILD_MANIFEST = $buildManifest
$env:SIYI_BUILD_INFO_LOCKED = '1'
& $python (Join-Path $root 'scripts\generate_build_info.py') --output $buildManifest --build-type Release
if ($LASTEXITCODE -ne 0) { throw 'Build manifest generation failed.' }
$metadataArguments = @((Join-Path $root 'scripts\check-release-metadata.py'))
if ($ReleaseEvidence) { $metadataArguments += '--release-preflight' }
& $python @metadataArguments
if ($LASTEXITCODE -ne 0) { throw 'Release metadata validation failed.' }
if ($ReleaseEvidence) {
    $manifest = Get-Content -LiteralPath $buildManifest -Raw -Encoding utf8 | ConvertFrom-Json
    $version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw -Encoding ascii).Trim()
    if ($EvidenceVersion) {
        $normalizedEvidenceVersion = $EvidenceVersion.Trim() -replace '^v(?=\d)', ''
        if ($normalizedEvidenceVersion -ne $version) {
            throw "Release evidence version $normalizedEvidenceVersion does not match VERSION=$version."
        }
    }
    $head = (git -C $root rev-parse HEAD).Trim()
    if (
        $manifest.product_version -ne $version -or
        $manifest.git_commit -ne $head -or
        $manifest.workspace_state -ne 'CLEAN' -or
        [string]$manifest.source_fingerprint -notmatch '^[0-9A-F]{64}$'
    ) {
        throw 'Release evidence requires a clean, current, version-synchronized build manifest.'
    }
    $sourceIdentity = [ordered]@{
        source_version = $version
        source_commit = $head
        source_tree_fingerprint = [string]$manifest.source_fingerprint
        workspace_clean = $true
        build_id = [string]$manifest.build_id
    }
    $env:SIYI_RELEASE_EVIDENCE_SOURCE_IDENTITY = $sourceIdentity | ConvertTo-Json -Compress
}
if (-not $CandidateOnly) {
    & $python -m pip install -r (Join-Path $backend 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed with exit code $LASTEXITCODE." }
    & $python -m pip install --no-deps -e $backend
    if ($LASTEXITCODE -ne 0) { throw "Backend package installation failed with exit code $LASTEXITCODE." }
}
Push-Location $backend
try {
    & $pyinstaller --noconfirm --clean --onedir --name agent-backend --add-data "${buildManifest};." --additional-hooks-dir $hookDirectory @sttHiddenImports --workpath $buildDirectory --distpath $distDirectory --specpath $specDirectory run_server.py
    $sidecarExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($sidecarExitCode -ne 0) { throw "Sidecar build failed with exit code $sidecarExitCode." }
New-Item -ItemType Directory -Force -Path $binaryDirectory | Out-Null
$builtSidecarDirectory = Join-Path $distDirectory 'agent-backend'
$sidecarSupportMayHaveChanged = $false
try {
Copy-Item -LiteralPath (Join-Path $builtSidecarDirectory 'agent-backend.exe') -Destination $target -Force
# Sync replaces the whole directory; mark before it so partial copy failures
# still restore the tracked placeholder during cleanup.
$sidecarSupportMayHaveChanged = $true
Sync-SidecarSupportDirectory (Join-Path $builtSidecarDirectory '_internal') $targetSupportDirectory $binaryDirectory
& $python (Join-Path $root 'scripts\check-frozen-artifacts.py') `
    --binary $target `
    --output (Join-Path $evidenceRoot 'frozen-artifacts.json')
if ($LASTEXITCODE -ne 0) { throw 'Frozen Artifact Engine inventory gate failed.' }

. (Join-Path $PSScriptRoot 'Import-MsvcEnvironment.ps1')
Push-Location $desktop
try {
    if (-not (Test-Path -LiteralPath $tauri)) {
        if ($CandidateOnly) { throw 'CandidateOnly cannot install missing frontend dependencies.' }
        npm.cmd --prefix frontend ci
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed with exit code $LASTEXITCODE." }
    }
    $cargoMetadataArguments = @('metadata', '--locked', '--manifest-path', 'src-tauri\Cargo.toml', '--format-version', '1')
    if ($CandidateOnly) { $cargoMetadataArguments += '--offline' }
    cargo @cargoMetadataArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Cargo lock validation failed with exit code $LASTEXITCODE." }
    if ($CandidateOnly) {
        # Reuse dependency compilation cache, but force OUR crate/build script
        # to consume the newly locked manifest instead of a cached build ID.
        # CARGO_TARGET_DIR is the fixed generated desktop/src-tauri/target.
        if ((Test-Path -LiteralPath $desktopTargetDirectory) -and
            ((Get-Item -LiteralPath $desktopTargetDirectory -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing to clean a linked desktop compilation cache.'
        }
        cargo clean --package app --release --manifest-path src-tauri\Cargo.toml --target-dir $desktopTargetDirectory
        if ($LASTEXITCODE -ne 0) { throw 'Candidate application-cache refresh failed.' }
    }
    $tauriBuildArguments = @('build', '--config', 'src-tauri\tauri.conf.json')
    if ($CandidateOnly) { $tauriBuildArguments += @('--config', $candidateTauriConfig, '--no-bundle', '--ci', '--', '--locked', '--offline') }
    & $tauri @tauriBuildArguments
    $desktopExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($desktopExitCode -ne 0) { throw "Desktop packaging failed with exit code $desktopExitCode." }
$runtimeApplicationName = "{0}{1}.exe" -f [char]0x53F8, [char]0x5FC6
$builtApplication = Join-Path $desktopTargetDirectory "release\$runtimeApplicationName"
$builtRuntimeSupportDirectory = if ($CandidateOnly) { Join-Path $builtSidecarDirectory '_internal' } else { Join-Path $desktopTargetDirectory 'release\_internal' }
$runtimeDirectory = $root
if ($CandidateOnly) {
    $candidateRoot = Resolve-CandidateDirectory $root $candidateRoot $candidateVersion -AllowExisting
    $runtimeDirectory = Join-Path $candidateRoot 'portable'
    New-Item -ItemType Directory -Path $runtimeDirectory | Out-Null
}
$runtimeApplication = Join-Path $runtimeDirectory $runtimeApplicationName
$runtimeSidecar = Join-Path $runtimeDirectory 'agent-backend.exe'
$runtimeSidecarSupportDirectory = Join-Path $runtimeDirectory '_internal'
if (-not (Test-Path -LiteralPath $builtApplication) -or -not (Test-Path -LiteralPath $builtRuntimeSupportDirectory -PathType Container)) {
    throw "Desktop build output is missing: $builtApplication"
}
Copy-Item -LiteralPath $builtApplication -Destination $runtimeApplication -Force
Copy-Item -LiteralPath $target -Destination $runtimeSidecar -Force
Sync-SidecarSupportDirectory $builtRuntimeSupportDirectory $runtimeSidecarSupportDirectory $runtimeDirectory
$expectedVersion = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$copiedVersion = (Get-Item -LiteralPath $runtimeApplication).VersionInfo.ProductVersion
if (-not $copiedVersion.StartsWith($expectedVersion, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Runtime version mismatch: expected $expectedVersion, found $copiedVersion."
}
if ($CandidateOnly) {
    $manifest = Get-Content -LiteralPath $buildManifest -Raw -Encoding utf8 | ConvertFrom-Json
    $packagedManifest = Join-Path $runtimeSidecarSupportDirectory 'build-info.json'
    if (-not (Test-Path -LiteralPath $packagedManifest -PathType Leaf) -or
        (Get-FileHash -LiteralPath $packagedManifest -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $buildManifest -Algorithm SHA256).Hash) {
        throw 'Portable candidate sidecar manifest differs from the locked build manifest.'
    }
    $candidateReceipt = [ordered]@{
        schema_version = 1
        report_type = 'desktop_candidate_build'
        actual_run = $true
        status = 'BUILT'
        scope = 'candidate_artifacts_only'
        release_status = 'NOT_RELEASED'
        runtime_smoke_status = 'NOT_RUN'
        installed_acceptance_status = 'NOT_RUN'
        dependency_policy = 'check_existing_only'
        product_version = [string]$manifest.product_version
        build_id = [string]$manifest.build_id
        source_commit = [string]$manifest.git_commit
        source_fingerprint = [string]$manifest.source_fingerprint
        workspace_state = [string]$manifest.workspace_state
        artifacts = @(
            @{ path = 'portable/' + $runtimeApplicationName; sha256 = (Get-FileHash -LiteralPath $runtimeApplication -Algorithm SHA256).Hash },
            @{ path = 'portable/agent-backend.exe'; sha256 = (Get-FileHash -LiteralPath $runtimeSidecar -Algorithm SHA256).Hash },
            @{ path = 'portable/_internal/build-info.json'; sha256 = (Get-FileHash -LiteralPath $packagedManifest -Algorithm SHA256).Hash }
        )
    }
    $candidateReceiptJson = $candidateReceipt | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText((Join-Path $candidateRoot 'candidate-build.json'), $candidateReceiptJson, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Candidate build complete: $runtimeDirectory"
    return
}
& (Join-Path $PSScriptRoot 'smoke-sidecar.ps1') -Binary $target -ArtifactSmoke -Output (Join-Path $evidenceRoot 'sidecar-smoke.json')
& (Join-Path $PSScriptRoot 'smoke-local-runtime-sidecar.ps1') -Binary $target -EvidenceVersion $EvidenceVersion -Output (Join-Path $evidenceRoot 'packaged-local-runtime-smoke.json')
if (-not $previousNsis) {
    throw 'The previous NSIS installer is required for upgrade and package-size validation.'
}
if (-not $PreviousMsiInstaller) {
    throw 'The previous MSI installer is required for upgrade validation.'
}
& (Join-Path $PSScriptRoot 'smoke-installer.ps1') -PreviousInstaller $previousNsis -Output (Join-Path $evidenceRoot 'nsis-installer-smoke.json')
& (Join-Path $PSScriptRoot 'smoke-msi.ps1') -PreviousInstaller $PreviousMsiInstaller -Output (Join-Path $evidenceRoot 'msi-installer-smoke.json')
$candidateInstaller = Get-ChildItem -LiteralPath (Join-Path $desktop 'src-tauri\target\release\bundle\nsis') `
    -Filter '*setup.exe' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (-not $candidateInstaller) { throw 'Candidate NSIS installer was not produced.' }
& $python (Join-Path $root 'scripts\check-package-size.py') `
    --candidate $candidateInstaller.FullName `
    --baseline $previousNsis `
    --output (Join-Path $evidenceRoot 'package-size.json')
if ($LASTEXITCODE -ne 0) { throw 'NSIS package-size gate failed.' }
if (-not $PerformanceBaselineBinary -and -not $AllowUnpairedPerformanceBaseline) {
    throw 'A clean previous-release sidecar is required for paired performance validation.'
}
$performanceArguments = @(
    (Join-Path $root 'scripts\run-v8-performance-gate.py'),
    '--output',
    (Join-Path $evidenceRoot 'performance-gate.json')
)
if ($PerformanceBaselineBinary) {
    $performanceArguments += @('--baseline-binary', $PerformanceBaselineBinary)
}
if (-not $AllowUnpairedPerformanceBaseline) {
    $performanceArguments += '--require-paired-baseline'
}
& $python @performanceArguments
if ($LASTEXITCODE -ne 0) { throw 'Performance gate failed.' }
& $python (Join-Path $root 'scripts\generate-sbom.py')
if ($LASTEXITCODE -ne 0) { throw 'SBOM generation failed.' }
& $python (Join-Path $root 'scripts\generate-third-party-notices.py')
if ($LASTEXITCODE -ne 0) { throw 'Third-party notice generation failed.' }
} finally {
    if ($sidecarSupportMayHaveChanged) {
        # Do this only after Tauri has consumed the complete onedir payload.
        Restore-TrackedSidecarSupportPlaceholder `
            -RepositoryRoot $root `
            -SupportDirectory $targetSupportDirectory `
            -AllowedRoot $binaryDirectory `
            -RelativePath $trackedSupportPlaceholderRelativePath
    }
}
} finally {
    if ($CandidateOnly) {
        foreach ($name in $previousCandidateEnvironment.Keys) {
            if ($null -eq $previousCandidateEnvironment[$name]) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            } else {
                [Environment]::SetEnvironmentVariable($name, $previousCandidateEnvironment[$name], 'Process')
            }
        }
    }
}
