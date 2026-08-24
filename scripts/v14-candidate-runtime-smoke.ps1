[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EvidenceVersion,
    # A fresh, simple identifier maps to candidate-runs/<RunId>.  It is the
    # preferred way to retain an immutable A22 candidate attachment beside
    # existing evidence.
    [string]$RunId = '',
    # An explicit path is relative to build/v{version}-evidence only.  It is
    # mutually exclusive with RunId so the selected output location is never
    # ambiguous.
    [string]$OutputDirectory = '',
    # Optional fresh raw JSON destination, relative to the v14 evidence root.
    # The release evidence runner attests this file directly so it must stay
    # separate from the one-shot candidate directory selected by RunId.
    [string]$AttestedOutput = '',
    # Preflight the selected output root without building a sidecar.  This is
    # intentionally useful to the release runner and focused path tests.
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'

# This is deliberately a candidate-only verification helper.  It creates a
# fresh frozen sidecar from the working tree, exercises it, and restores only
# the temporary Tauri sidecar staging payload even when a build or smoke test
# fails.  It never creates an installer, changes VERSION, or overwrites the
# user's root portable runtime.
$repositoryRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$evidenceRoot = & (Join-Path $PSScriptRoot 'evidence-root.ps1') -RepositoryRoot $repositoryRoot -EvidenceVersion $EvidenceVersion
$repositoryRoot = [System.IO.Path]::GetFullPath($repositoryRoot)
$evidenceRoot = [System.IO.Path]::GetFullPath($evidenceRoot)

function Test-ReparsePoint([System.IO.FileSystemInfo]$Item) {
    if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        return $true
    }
    $linkType = $Item.PSObject.Properties['LinkType']
    return $null -ne $linkType -and -not [string]::IsNullOrWhiteSpace([string]$linkType.Value)
}

function Get-ChildPathUnderRoot([string]$Path, [string]$Root, [string]$Description) {
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $rootBoundary = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    if ($pathFull -eq $rootFull -or -not $pathFull.StartsWith($rootBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Description must remain below ${rootFull}: $pathFull"
    }
    return $pathFull
}

function Test-PathIsAtOrBelowRoot([string]$Path, [string]$Root) {
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/'))
    $rootBoundary = $rootFull + [System.IO.Path]::DirectorySeparatorChar
    return $pathFull -eq $rootFull -or $pathFull.StartsWith($rootBoundary, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-PlainDirectory([string]$Path, [string]$Description) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or (Test-ReparsePoint $item)) {
        throw "Refusing linked or non-directory ${Description}: $Path"
    }
}

function Assert-SafeDirectoryChain([string]$Path, [string]$AllowedRoot) {
    $pathFull = Get-ChildPathUnderRoot $Path $AllowedRoot 'Candidate output path'
    Assert-PlainDirectory $AllowedRoot 'candidate output root'
    $allowedRootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([char[]]@('\', '/'))
    $relative = $pathFull.Substring($allowedRootFull.Length).TrimStart([char[]]@('\', '/'))
    $current = $allowedRootFull
    foreach ($segment in ($relative -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "Candidate output path has an empty segment: $Path"
        }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            return
        }
        Assert-PlainDirectory $current 'candidate output path component'
    }
}

function Ensure-SafeDirectoryChain([string]$Path, [string]$AllowedRoot) {
    $pathFull = Get-ChildPathUnderRoot $Path $AllowedRoot 'Candidate output path'
    Assert-PlainDirectory $AllowedRoot 'candidate output root'
    $allowedRootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([char[]]@('\', '/'))
    $relative = $pathFull.Substring($allowedRootFull.Length).TrimStart([char[]]@('\', '/'))
    $current = $allowedRootFull
    foreach ($segment in ($relative -split '[\\/]')) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "Candidate output path has an empty segment: $Path"
        }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            # $current has already been constrained below AllowedRoot and
            # every path component was checked before this supported -Path
            # creation call.
            New-Item -ItemType Directory -Path $current -ErrorAction Stop | Out-Null
        }
        Assert-PlainDirectory $current 'candidate output path component'
    }
}

function New-SafeFreshDirectory([string]$Path, [string]$AllowedRoot) {
    $pathFull = Get-ChildPathUnderRoot $Path $AllowedRoot 'Candidate output directory'
    if (Test-Path -LiteralPath $pathFull) {
        throw "Refusing to reuse an existing candidate output directory: $pathFull"
    }
    Ensure-SafeDirectoryChain $pathFull $AllowedRoot
}

function Assert-SafeDirectoryAtOrBelowRoot([string]$Path, [string]$AllowedRoot) {
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([char[]]@('\', '/'))
    if ($pathFull -eq $rootFull) {
        Assert-PlainDirectory $rootFull 'candidate output root'
        return
    }
    Assert-SafeDirectoryChain $pathFull $rootFull
}

function Ensure-SafeDirectoryAtOrBelowRoot([string]$Path, [string]$AllowedRoot) {
    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([char[]]@('\', '/'))
    if ($pathFull -eq $rootFull) {
        Assert-PlainDirectory $rootFull 'candidate output root'
        return
    }
    Ensure-SafeDirectoryChain $pathFull $rootFull
}

function Resolve-RequestedCandidateRoot {
    $hasRunId = -not [string]::IsNullOrWhiteSpace($RunId)
    $hasOutputDirectory = -not [string]::IsNullOrWhiteSpace($OutputDirectory)
    if ($hasRunId -and $hasOutputDirectory) {
        throw 'RunId and OutputDirectory are mutually exclusive.'
    }

    if ($hasRunId) {
        if ($RunId -ne $RunId.Trim() -or $RunId.EndsWith('.') -or $RunId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$') {
            throw "RunId must contain only letters, digits, dot, underscore, or hyphen: '$RunId'"
        }
        return [System.IO.Path]::GetFullPath((Join-Path (Join-Path $evidenceRoot 'candidate-runs') $RunId))
    }

    if ($hasOutputDirectory) {
        if ($OutputDirectory -ne $OutputDirectory.Trim() -or [System.IO.Path]::IsPathRooted($OutputDirectory) -or $OutputDirectory.Contains(':')) {
            throw "OutputDirectory must be a relative path below build/v*-evidence: '$OutputDirectory'"
        }
        $segments = $OutputDirectory -split '[\\/]'
        if ($segments.Count -eq 0) {
            throw "OutputDirectory is empty: '$OutputDirectory'"
        }
        foreach ($segment in $segments) {
            if ([string]::IsNullOrWhiteSpace($segment) -or $segment -ne $segment.Trim() -or $segment.EndsWith('.') -or $segment -in @('.', '..') -or $segment.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $segment.IndexOfAny([char[]]@('[', ']')) -ge 0) {
                throw "OutputDirectory has an unsafe path segment: '$OutputDirectory'"
            }
        }
        return [System.IO.Path]::GetFullPath((Join-Path $evidenceRoot $OutputDirectory))
    }

    return [System.IO.Path]::GetFullPath((Join-Path $evidenceRoot 'candidate-optimized'))
}

$candidateRoot = Resolve-RequestedCandidateRoot
$candidateRoot = Get-ChildPathUnderRoot $candidateRoot $evidenceRoot 'Candidate output directory'
$isolatedOutput = -not [string]::IsNullOrWhiteSpace($RunId) -or -not [string]::IsNullOrWhiteSpace($OutputDirectory)
$hasAttestedOutput = -not [string]::IsNullOrEmpty($AttestedOutput)
$attestationRawOutput = $null
if ($hasAttestedOutput) {
    if ($AttestedOutput -ne $AttestedOutput.Trim() -or [System.IO.Path]::IsPathRooted($AttestedOutput) -or $AttestedOutput.Contains(':') -or $AttestedOutput.EndsWith('\') -or $AttestedOutput.EndsWith('/')) {
        throw "AttestedOutput must be a non-empty relative .json path below build/v*-evidence: '$AttestedOutput'"
    }
    $attestedSegments = $AttestedOutput -split '[\\/]'
    foreach ($segment in $attestedSegments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -ne $segment.Trim() -or $segment.EndsWith('.') -or $segment -in @('.', '..') -or $segment.IndexOfAny([System.IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $segment.IndexOfAny([char[]]@('[', ']')) -ge 0) {
            throw "AttestedOutput has an unsafe path segment: '$AttestedOutput'"
        }
    }
    $attestationRawOutput = [System.IO.Path]::GetFullPath((Join-Path $evidenceRoot $AttestedOutput))
    $attestationRawOutput = Get-ChildPathUnderRoot $attestationRawOutput $evidenceRoot 'Attested output'
    if (-not $attestationRawOutput.EndsWith('.json', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "AttestedOutput must end with .json: '$AttestedOutput'"
    }
    if (Test-PathIsAtOrBelowRoot $attestationRawOutput $candidateRoot) {
        throw "AttestedOutput must remain outside the candidate output directory: $attestationRawOutput"
    }
}
$attestationRawParent = if ($hasAttestedOutput) { Split-Path -Parent $attestationRawOutput } else { $null }

# The evidence root itself is allowed to be created for a real run, but a
# preflight must not make any filesystem changes.
if ($ValidateOnly) {
    Assert-SafeDirectoryChain $evidenceRoot $repositoryRoot
    Assert-SafeDirectoryChain $candidateRoot $evidenceRoot
    if ($hasAttestedOutput) {
        Assert-SafeDirectoryAtOrBelowRoot $attestationRawParent $evidenceRoot
        if (Test-Path -LiteralPath $attestationRawOutput) {
            throw "Refusing to overwrite an existing attested output: $attestationRawOutput"
        }
    }
    if ($isolatedOutput -and (Test-Path -LiteralPath $candidateRoot)) {
        throw "Refusing to reuse an existing candidate output directory: $candidateRoot"
    }
} else {
    Ensure-SafeDirectoryChain $evidenceRoot $repositoryRoot
    if ($hasAttestedOutput) {
        Assert-SafeDirectoryAtOrBelowRoot $attestationRawParent $evidenceRoot
        if (Test-Path -LiteralPath $attestationRawOutput) {
            throw "Refusing to overwrite an existing attested output: $attestationRawOutput"
        }
    }
    if ($isolatedOutput) {
        New-SafeFreshDirectory $candidateRoot $evidenceRoot
    } else {
        Assert-SafeDirectoryChain $candidateRoot $evidenceRoot
    }
    if ($hasAttestedOutput) {
        Ensure-SafeDirectoryAtOrBelowRoot $attestationRawParent $evidenceRoot
    }
}

$baselineRoot = Join-Path $evidenceRoot 'pre-v14-runtime-backup'
# A fresh run reads the verified prior-release binary from its historical
# baseline but keeps *restore* snapshots under the new candidate root.  This
# prevents the candidate run from editing either candidate-optimized or the
# retained baseline attachment.
$backupRoot = if ($isolatedOutput) { Join-Path $candidateRoot 'runtime-restore-backup' } else { $baselineRoot }
$candidateSidecarDirectory = Join-Path $candidateRoot 'sidecar'
$candidateSidecar = Join-Path $candidateSidecarDirectory 'agent-backend.exe'
$cargoTarget = if ($isolatedOutput) { Join-Path $candidateRoot 'cargo-target-optimized' } else { Join-Path $evidenceRoot 'cargo-target-optimized' }
$performanceBaseline = Join-Path $baselineRoot 'agent-backend-x86_64-pc-windows-msvc.exe'
$stagingSidecarBackup = Get-ChildPathUnderRoot (Join-Path $backupRoot 'staging-agent-backend-x86_64-pc-windows-msvc.exe') $backupRoot 'Candidate sidecar backup'
$frozenArtifactsOutput = Join-Path $candidateRoot 'frozen-artifacts.json'
$localRuntimeSmokeOutput = Join-Path $candidateRoot 'packaged-local-runtime-smoke.json'
$artifactSmokeOutput = Join-Path $candidateRoot 'sidecar-artifact-smoke.json'
$candidatePerformanceOutput = Join-Path $candidateRoot 'sidecar-performance.json'
$performanceOutput = if ($hasAttestedOutput) { $attestationRawOutput } else { $candidatePerformanceOutput }

if ($isolatedOutput) {
    foreach ($path in @($backupRoot, $stagingSidecarBackup, $candidateSidecarDirectory, $cargoTarget, $frozenArtifactsOutput, $localRuntimeSmokeOutput, $artifactSmokeOutput, $candidatePerformanceOutput)) {
        $pathFull = Get-ChildPathUnderRoot $path $candidateRoot 'Fresh candidate output path'
        if (Test-Path -LiteralPath $pathFull) {
            throw "Refusing to overwrite a fresh candidate output path: $pathFull"
        }
    }
}

if ($ValidateOnly) {
    Write-Output (ConvertTo-Json ([ordered]@{
        status = 'VALID'
        output_mode = if ($isolatedOutput) { 'isolated' } else { 'default' }
        candidate_root = $candidateRoot
        performance_output = $performanceOutput
        attested_output = $attestationRawOutput
        cargo_target = $cargoTarget
    }))
    return
}

Assert-SafeDirectoryChain $baselineRoot $evidenceRoot
if ($isolatedOutput) {
    Ensure-SafeDirectoryChain $backupRoot $candidateRoot
}

$sidecar = Join-Path $repositoryRoot 'desktop\src-tauri\binaries\agent-backend-x86_64-pc-windows-msvc.exe'
$sidecarSupportDirectory = Join-Path $repositoryRoot 'desktop\src-tauri\binaries\_internal'
$supportBackups = @(
    [pscustomobject]@{
        Destination = $sidecarSupportDirectory
        Backup = Join-Path $backupRoot 'sidecar-_internal'
        AllowedRoot = Join-Path $repositoryRoot 'desktop\src-tauri\binaries'
        Existed = $false
    }
)

function Assert-SupportDestination([string]$Destination, [string]$AllowedRoot) {
    $destinationFull = [System.IO.Path]::GetFullPath($Destination)
    $allowedRootFull = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not ($destinationFull + [System.IO.Path]::DirectorySeparatorChar).StartsWith($allowedRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to mutate sidecar support outside its allowed directory: $destinationFull"
    }
}

function Snapshot-SupportDirectory($Entry) {
    Assert-SupportDestination $Entry.Destination $Entry.AllowedRoot
    Assert-SupportDestination $Entry.Backup $backupRoot
    if (Test-Path -LiteralPath $Entry.Backup) {
        $backupItem = Get-Item -LiteralPath $Entry.Backup -Force
        if (-not $backupItem.PSIsContainer -or (Test-ReparsePoint $backupItem)) {
            throw "Refusing to replace linked or non-directory sidecar support backup: $($Entry.Backup)"
        }
        if ($isolatedOutput) {
            throw "Refusing to reuse a fresh-run sidecar support backup: $($Entry.Backup)"
        }
        Remove-Item -LiteralPath $Entry.Backup -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath $Entry.Destination)) {
        $Entry.Existed = $false
        return
    }
    $item = Get-Item -LiteralPath $Entry.Destination -Force
    if (-not $item.PSIsContainer -or (Test-ReparsePoint $item)) {
        throw "Refusing to snapshot linked or non-directory sidecar support: $($Entry.Destination)"
    }
    Copy-Item -LiteralPath $Entry.Destination -Destination $Entry.Backup -Recurse -Force
    $Entry.Existed = $true
}

function Restore-SupportDirectory($Entry) {
    Assert-SupportDestination $Entry.Destination $Entry.AllowedRoot
    if (Test-Path -LiteralPath $Entry.Destination) {
        $item = Get-Item -LiteralPath $Entry.Destination -Force
        if (-not $item.PSIsContainer -or (Test-ReparsePoint $item)) {
            throw "Refusing to replace linked or non-directory sidecar support: $($Entry.Destination)"
        }
        Remove-Item -LiteralPath $Entry.Destination -Recurse -Force
    }
    if ($Entry.Existed) {
        if (-not (Test-Path -LiteralPath $Entry.Backup -PathType Container)) {
            throw "The baseline sidecar support backup is missing: $($Entry.Backup)"
        }
        Copy-Item -LiteralPath $Entry.Backup -Destination $Entry.Destination -Recurse -Force
    }
}

function Snapshot-CandidateSidecarPayload {
    Assert-SupportDestination $candidateSidecarDirectory $candidateRoot
    if (Test-Path -LiteralPath $candidateSidecarDirectory) {
        $existing = Get-Item -LiteralPath $candidateSidecarDirectory -Force
        if (-not $existing.PSIsContainer -or (Test-ReparsePoint $existing)) {
            throw "Refusing to replace linked or non-directory candidate sidecar payload: $candidateSidecarDirectory"
        }
        if ($isolatedOutput) {
            throw "Refusing to reuse a fresh-run candidate sidecar payload: $candidateSidecarDirectory"
        }
        Remove-Item -LiteralPath $candidateSidecarDirectory -Recurse -Force
    }
    if (-not (Test-Path -LiteralPath $sidecarSupportDirectory -PathType Container)) {
        throw "The built candidate sidecar support directory is missing: $sidecarSupportDirectory"
    }
    $linkedSupport = Get-ChildItem -LiteralPath $sidecarSupportDirectory -Force -Recurse | Where-Object { Test-ReparsePoint $_ }
    if ($linkedSupport) {
        throw "Refusing to preserve linked candidate sidecar support entries from $sidecarSupportDirectory"
    }
    if ($isolatedOutput) {
        # The selected candidate root and this fixed child name were checked
        # before the real build begins; its creation cmdlet exposes Path only
        # in Windows PowerShell.
        New-Item -ItemType Directory -Path $candidateSidecarDirectory -ErrorAction Stop | Out-Null
    } else {
        New-Item -ItemType Directory -Force -Path $candidateSidecarDirectory | Out-Null
    }
    Copy-Item -LiteralPath $sidecar -Destination $candidateSidecar -Force
    Copy-Item -LiteralPath $sidecarSupportDirectory -Destination (Join-Path $candidateSidecarDirectory '_internal') -Recurse -Force
}

if (-not (Test-Path -LiteralPath $sidecar) -or -not (Test-Path -LiteralPath $performanceBaseline)) {
    throw "A required runtime artifact or its verified performance baseline is missing: $sidecar"
}

$sidecarItem = Get-Item -LiteralPath $sidecar -Force
if ($sidecarItem.PSIsContainer -or (Test-ReparsePoint $sidecarItem)) {
    throw "Refusing to snapshot linked or non-file sidecar staging binary: $sidecar"
}
if (Test-Path -LiteralPath $stagingSidecarBackup) {
    $backupItem = Get-Item -LiteralPath $stagingSidecarBackup -Force
    if ($backupItem.PSIsContainer -or (Test-ReparsePoint $backupItem)) {
        throw "Refusing to replace linked or non-file candidate sidecar backup: $stagingSidecarBackup"
    }
    if ($isolatedOutput) {
        throw "Refusing to reuse a fresh-run candidate sidecar backup: $stagingSidecarBackup"
    }
    Remove-Item -LiteralPath $stagingSidecarBackup -Force
}
$stagingSidecarHash = (Get-FileHash -LiteralPath $sidecar -Algorithm SHA256).Hash
Copy-Item -LiteralPath $sidecar -Destination $stagingSidecarBackup -Force
foreach ($entry in $supportBackups) {
    Snapshot-SupportDirectory $entry
}

$completed = $false
try {
    & (Join-Path $PSScriptRoot 'build-runtime.ps1') -Force -SkipPortableRuntimeSync -CargoTargetDirectory $cargoTarget
    if ($LASTEXITCODE -ne 0) { throw 'Optimized candidate runtime build failed.' }

    Snapshot-CandidateSidecarPayload

    & (Join-Path $repositoryRoot 'siyi\.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'check-frozen-artifacts.py') `
        --binary $candidateSidecar `
        --output $frozenArtifactsOutput
    if ($LASTEXITCODE -ne 0) { throw 'Frozen artifact inventory failed.' }

    & (Join-Path $PSScriptRoot 'smoke-local-runtime-sidecar.ps1') `
        -Binary $candidateSidecar `
        -EvidenceVersion $EvidenceVersion `
        -Output $localRuntimeSmokeOutput
    if ($LASTEXITCODE -ne 0) { throw 'Packaged local-runtime smoke failed.' }

    & (Join-Path $PSScriptRoot 'smoke-sidecar.ps1') `
        -Binary $candidateSidecar `
        -ArtifactSmoke `
        -Output $artifactSmokeOutput
    if ($LASTEXITCODE -ne 0) { throw 'Candidate packaged Artifact Engine smoke failed.' }

    # The v14 plan requires distinct three-run median and ten-run P95 samples,
    # plus an on-machine prior-release comparison.  The helper records the
    # candidate binary and adjacent onedir payload hashes; the outer v14
    # evidence runner then binds this real command to the source fingerprint.
    & (Join-Path $repositoryRoot 'siyi\.venv\Scripts\python.exe') `
        (Join-Path $PSScriptRoot 'v14-sidecar-performance.py') `
        --candidate $candidateSidecar `
        --baseline $performanceBaseline `
        --output $performanceOutput
    if ($LASTEXITCODE -ne 0) { throw 'Candidate v14 sidecar performance contract failed.' }

    $completed = $true
    Write-Output (ConvertTo-Json ([ordered]@{
        status = 'PASS'
        candidate_root = $candidateRoot
        candidate_sidecar = $candidateSidecar
        cargo_target = $cargoTarget
        baseline_restored = $true
    }))
} finally {
    Copy-Item -LiteralPath $stagingSidecarBackup -Destination $sidecar -Force
    $restoredHash = (Get-FileHash -LiteralPath $sidecar -Algorithm SHA256).Hash
    if ($restoredHash -ne $stagingSidecarHash) {
        throw "Failed to restore the original sidecar staging binary: $sidecar"
    }
    foreach ($entry in $supportBackups) {
        Restore-SupportDirectory $entry
    }
    if (-not $completed) {
        Write-Error 'Candidate runtime verification failed; verified v13 runtime artifacts were restored.'
    }
}
