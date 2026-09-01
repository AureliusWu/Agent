param(
    [string]$BundleDirectory = '',
    [string]$PreviousInstaller = '',
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$tauriConfig = Get-Content -LiteralPath (Join-Path $root 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding utf8 | ConvertFrom-Json
$productName = [string]$tauriConfig.productName
$applicationName = "{0}.exe" -f [string]$tauriConfig.mainBinaryName
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$fixtureScript = Join-Path $root 'scripts\upgrade-database-fixture.py'
$testInstallRegistry = "HKCU:\Software\github\$productName"
if (-not (Test-Path -LiteralPath $python)) { throw 'Backend Python environment is required for the upgrade fixture.' }
$previousDeploymentMode = [Environment]::GetEnvironmentVariable('AGENT_DEPLOYMENT_MODE', 'Process')
try {
    $env:AGENT_DEPLOYMENT_MODE = 'desktop_local'
    $schemaVersion = [int](& $python -c "import sys; sys.path.insert(0, r'$($root)\siyi'); from app.database import SCHEMA_VERSION; print(SCHEMA_VERSION)")
} finally {
    [Environment]::SetEnvironmentVariable('AGENT_DEPLOYMENT_MODE', $previousDeploymentMode, 'Process')
}
if ($LASTEXITCODE -ne 0 -or $schemaVersion -lt 2) { throw 'Could not determine the current database schema version.' }
$fixtureSchemaVersion = $schemaVersion - 1
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $root 'desktop\src-tauri\target\release\bundle'
}
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$nsis = Get-ChildItem -LiteralPath (Join-Path $bundle 'nsis') -Filter "${productName}_${version}_*-setup.exe" | Select-Object -First 1
$msi = Get-ChildItem -LiteralPath (Join-Path $bundle 'msi') -Filter "${productName}_${version}_*.msi" | Select-Object -First 1
if (-not $nsis -or -not $msi) { throw 'Expected both NSIS and MSI installers.' }
if ($nsis.Length -lt 1MB -or $msi.Length -lt 1MB) { throw 'Installer output is unexpectedly small.' }

$installDirectory = Join-Path $env:TEMP ('agent-installer-smoke-' + [Guid]::NewGuid().ToString('N'))
$dataDirectory = Join-Path $env:TEMP ('agent-data-smoke-' + [Guid]::NewGuid().ToString('N'))
$applicationProcess = $null
$sidecarProcessId = $null
$uninstalled = $false
$runId = [Guid]::NewGuid().ToString('N')
$startedAt = [DateTime]::UtcNow.ToString('o')
$sourceIdentity = $null
$releaseSourceIdentityJson = [Environment]::GetEnvironmentVariable('SIYI_RELEASE_EVIDENCE_SOURCE_IDENTITY', 'Process')
$legacySourceIdentityJson = [Environment]::GetEnvironmentVariable('SIYI_V14_EVIDENCE_SOURCE_IDENTITY', 'Process')
$legacyEvidenceMode = [string]::IsNullOrWhiteSpace($releaseSourceIdentityJson) -and -not [string]::IsNullOrWhiteSpace($legacySourceIdentityJson)
if ($legacyEvidenceMode -and $version -ne '14.0.0') {
    throw 'The SIYI_V14_EVIDENCE_SOURCE_IDENTITY compatibility alias is valid only for v14 evidence and cannot authorize a current release.'
}
$sourceIdentityJson = if (-not [string]::IsNullOrWhiteSpace($releaseSourceIdentityJson)) {
    $releaseSourceIdentityJson
} else {
    $legacySourceIdentityJson
}
if (-not [string]::IsNullOrWhiteSpace($sourceIdentityJson)) {
    try {
        $sourceIdentity = $sourceIdentityJson | ConvertFrom-Json
    } catch {
        throw 'The release evidence runner supplied an invalid source identity.'
    }
    foreach ($field in @('source_version', 'source_commit', 'source_tree_fingerprint', 'workspace_clean')) {
        if ($null -eq $sourceIdentity.$field) {
            throw "The release evidence runner source identity is missing $field."
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($releaseSourceIdentityJson) -and -not [string]::IsNullOrWhiteSpace($legacySourceIdentityJson)) {
        $legacyIdentity = $legacySourceIdentityJson | ConvertFrom-Json
        foreach ($field in @('source_version', 'source_commit', 'source_tree_fingerprint', 'workspace_clean')) {
            if ([string]$legacyIdentity.$field -ne [string]$sourceIdentity.$field) {
                throw 'Conflicting generic and legacy release source identities were supplied.'
            }
        }
    }
    if (-not $Output) { throw 'A runner-bound NSIS evidence run requires -Output.' }
}
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($currentIdentity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Get-InstallerArtifactIdentity {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$File,
        [Parameter(Mandatory = $true)][string]$ExpectedSuffix,
        [bool]$AllowCanonicalReleaseName = $false
    )
    if (-not $File.Name.EndsWith($ExpectedSuffix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Installer has the wrong package kind: $($File.FullName)"
    }
    $productPattern = if ($AllowCanonicalReleaseName) {
        '(?:' + [Regex]::Escape($productName) + '|Siyi)'
    } else {
        [Regex]::Escape($productName)
    }
    $pattern = '^' + $productPattern + '_(?<version>\d+\.\d+\.\d+)_'
    $match = [Regex]::Match($File.Name, $pattern, [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) {
        throw "Installer filename does not expose a stable product version: $($File.Name)"
    }
    return [ordered]@{
        name = $File.Name
        version = $match.Groups['version'].Value
        bytes = $File.Length
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash
    }
}

function Assert-InstalledBuildIdentity {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$Sidecar,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [bool]$RequireCurrentSource,
        [bool]$AllowLegacyOneFile = $false
    )
    $manifestPath = Join-Path $Sidecar.DirectoryName '_internal\build-info.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        if (-not $AllowLegacyOneFile) {
            throw 'Installed NSIS candidate does not contain its adjacent onedir build manifest.'
        }
        $archiveReader = Join-Path $root 'scripts\read-pyinstaller-build-info.py'
        if (-not (Test-Path -LiteralPath $archiveReader -PathType Leaf)) {
            throw 'Legacy onefile build-manifest reader is missing.'
        }
        $archiveOutput = & $python $archiveReader $Sidecar.FullName 2>$null
        $archiveExitCode = $LASTEXITCODE
        if ($archiveExitCode -ne 0) {
            throw 'Could not safely read the installed legacy onefile build manifest.'
        }
        try {
            $embeddedManifest = (@($archiveOutput) -join "`n") | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw 'Legacy onefile build-manifest reader returned invalid JSON.'
        }
        try {
            if ($embeddedManifest.embedded_manifest_bytes -is [bool]) {
                throw 'embedded manifest bytes must be numeric.'
            }
            $embeddedManifestBytes = [int64]$embeddedManifest.embedded_manifest_bytes
        } catch {
            throw 'Legacy onefile build-manifest reader returned invalid manifest bytes.'
        }
        if (
            $embeddedManifest.archive_entry -ne 'build-info.json' -or
            $embeddedManifest.product_version -ne $ExpectedVersion -or
            $embeddedManifest.workspace_state -ne 'CLEAN' -or
            [string]$embeddedManifest.git_commit -notmatch '^[0-9a-f]{40}$' -or
            [string]$embeddedManifest.source_fingerprint -notmatch '^[0-9a-f]{64}$' -or
            -not $embeddedManifest.build_id -or
            $embeddedManifest.component_build_id -ne ('sidecar-' + [string]$embeddedManifest.build_id) -or
            $embeddedManifestBytes -le 0 -or
            $embeddedManifestBytes -gt (128KB) -or
            [string]$embeddedManifest.embedded_manifest_sha256 -notmatch '^[0-9A-F]{64}$'
        ) {
            throw 'Installed legacy onefile build manifest does not match its prior installer artifact.'
        }
        return [ordered]@{
            identity_mode = 'legacy_onefile_embedded_manifest'
            product_version = [string]$embeddedManifest.product_version
            git_commit = [string]$embeddedManifest.git_commit
            source_fingerprint = [string]$embeddedManifest.source_fingerprint
            workspace_state = [string]$embeddedManifest.workspace_state
            build_id = [string]$embeddedManifest.build_id
            component_build_id = [string]$embeddedManifest.component_build_id
            embedded_manifest_entry = [string]$embeddedManifest.archive_entry
            embedded_manifest_bytes = $embeddedManifestBytes
            embedded_manifest_sha256 = [string]$embeddedManifest.embedded_manifest_sha256
            executable_name = $Sidecar.Name
            executable_bytes = $Sidecar.Length
            executable_sha256 = (Get-FileHash -LiteralPath $Sidecar.FullName -Algorithm SHA256).Hash
        }
    }
    $manifestFile = Get-Item -LiteralPath $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if (
        $manifest.product_version -ne $ExpectedVersion -or
        $manifest.workspace_state -ne 'CLEAN' -or
        [string]$manifest.git_commit -notmatch '^[0-9a-f]{40}$' -or
        [string]$manifest.source_fingerprint -notmatch '^[0-9a-f]{64}$' -or
        -not $manifest.build_id -or
        -not $manifest.component_build_ids.sidecar
    ) {
        throw 'Installed NSIS package build identity does not match its installer artifact version.'
    }
    if (
        $RequireCurrentSource -and
        (
            $manifest.git_commit -ne $sourceIdentity.source_commit -or
            $manifest.source_fingerprint -ne $sourceIdentity.source_tree_fingerprint -or
            $manifest.workspace_state -ne 'CLEAN'
        )
    ) {
        throw 'Installed NSIS candidate build identity does not match the runner-bound clean source.'
    }
    return [ordered]@{
        product_version = [string]$manifest.product_version
        git_commit = [string]$manifest.git_commit
        source_fingerprint = [string]$manifest.source_fingerprint
        workspace_state = [string]$manifest.workspace_state
        build_id = [string]$manifest.build_id
        component_build_id = [string]$manifest.component_build_ids.sidecar
        bytes = $manifestFile.Length
        sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash
    }
}

$candidateArtifact = Get-InstallerArtifactIdentity -File $nsis -ExpectedSuffix '-setup.exe'
$initialInstaller = $nsis.FullName
$previousArtifact = $null
if ($PreviousInstaller) {
    $initialInstaller = (Resolve-Path -LiteralPath $PreviousInstaller).Path
    $previousFile = Get-Item -LiteralPath $initialInstaller
    $previousArtifact = Get-InstallerArtifactIdentity -File $previousFile -ExpectedSuffix '-setup.exe' -AllowCanonicalReleaseName $true
}
if ($sourceIdentity) {
    if ($sourceIdentity.source_version -ne $version) {
        throw "Runner-bound NSIS acceptance requires synchronized $version source and package metadata."
    }
    if ($sourceIdentity.workspace_clean -ne $true) {
        throw 'Runner-bound NSIS acceptance requires a clean source workspace.'
    }
    if ($legacyEvidenceMode -and $isAdministrator) {
        throw 'A27 NSIS acceptance must run from a non-administrator process.'
    }
    if (-not $previousArtifact) {
        throw 'A27 NSIS acceptance requires -PreviousInstaller for a real prior-version upgrade.'
    }
    if (
        [version]$previousArtifact.version -ge [version]$candidateArtifact.version -or
        $previousArtifact.sha256 -eq $candidateArtifact.sha256
    ) {
        throw 'A27 previous NSIS installer must be a distinct version older than the current candidate.'
    }
}

function Write-JsonResult {
    param(
        [Parameter(Mandatory = $true)][string]$Json,
        [string]$Destination,
        [bool]$Immutable
    )
    if (-not $Destination) { return }
    $outputPath = if ([System.IO.Path]::IsPathRooted($Destination)) {
        [System.IO.Path]::GetFullPath($Destination)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $root $Destination))
    }
    if ($Immutable) {
        $evidenceRoot = [System.IO.Path]::GetFullPath((& (Join-Path $PSScriptRoot 'evidence-root.ps1') -RepositoryRoot $root -EvidenceVersion $version))
        $evidenceBoundary = $evidenceRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not ($outputPath + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
            $evidenceBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Runner-bound NSIS evidence must stay under the v$version evidence root."
        }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
    if ($Immutable) {
        $encoding = [System.Text.UTF8Encoding]::new($false)
        $bytes = $encoding.GetBytes($Json + [Environment]::NewLine)
        try {
            $stream = [System.IO.File]::Open(
                $outputPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        } catch [System.IO.IOException] {
            throw "Refusing to overwrite immutable NSIS evidence: $outputPath"
        }
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally {
            $stream.Dispose()
        }
    } else {
        Set-Content -LiteralPath $outputPath -Value $Json -Encoding utf8
    }
}

function Remove-TestOwnedInstallRegistry {
    param([string]$ExpectedDirectory)
    if (-not (Test-Path -LiteralPath $testInstallRegistry)) { return }
    $registryItem = Get-Item -LiteralPath $testInstallRegistry -ErrorAction SilentlyContinue
    $registered = [string]$registryItem.GetValue('InstallDir', $null)
    if (-not $registered) {
        $registered = [string]$registryItem.GetValue('', $null)
    }
    if (-not $registered) { return }
    $resolved = [System.IO.Path]::GetFullPath($registered).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $expected = [System.IO.Path]::GetFullPath($ExpectedDirectory).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $tempBoundary = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (
        $resolved.Equals($expected, [System.StringComparison]::OrdinalIgnoreCase) -and
        ($resolved + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
            $tempBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        (Split-Path -Leaf $resolved) -like 'agent-installer-smoke-*'
    ) {
        Remove-Item -LiteralPath $testInstallRegistry -Recurse -Force
    }
}

try {
    $installArguments = @('/S', "/D=$installDirectory")
    $install = Start-Process -FilePath $initialInstaller -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) { throw "NSIS installation failed with exit code $($install.ExitCode)." }
    $previousBuildIdentity = $null
    if ($sourceIdentity) {
        $previousApplication = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
            Select-Object -First 1
        if (-not $previousApplication) { throw 'Previous NSIS installation did not produce the desktop executable.' }
        $previousSidecar = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter 'agent-backend*.exe' |
            Select-Object -First 1
        if (-not $previousSidecar) { throw 'Previous NSIS installation did not produce the backend sidecar.' }
        $previousBuildIdentity = Assert-InstalledBuildIdentity `
            -Sidecar $previousSidecar `
            -ExpectedVersion ([string]$previousArtifact.version) `
            -RequireCurrentSource $false `
            -AllowLegacyOneFile $true
        if ($previousBuildIdentity['identity_mode'] -eq 'legacy_onefile_embedded_manifest') {
            $previousBuildIdentity['installer_version'] = [string]$previousArtifact.version
            $previousBuildIdentity['installer_sha256'] = [string]$previousArtifact.sha256
        }
    }

    New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
    $database = Join-Path $dataDirectory 'data\agent.db'
    & $python $fixtureScript create $database --schema $fixtureSchemaVersion | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the schema $fixtureSchemaVersion upgrade fixture." }

    $previousVersionUpgrade = $false
    if ($PreviousInstaller) {
        $candidateInstall = Start-Process -FilePath $nsis.FullName -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
        if ($candidateInstall.ExitCode -ne 0) { throw "Candidate upgrade failed with exit code $($candidateInstall.ExitCode)." }
        if (Test-Path -LiteralPath (Join-Path $installDirectory 'Agent.exe')) {
            throw 'Candidate upgrade left the legacy Agent.exe beside 司忆.exe.'
        }
        $previousVersionUpgrade = $true
    }
    $executables = @(Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter '*.exe')
    $application = $executables | Where-Object { $_.Name -ieq $applicationName } | Select-Object -First 1
    $sidecar = $executables | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
    if (-not $application -or -not $sidecar) {
        throw "Installed application or backend sidecar is missing. Found: $($executables.Name -join ', ')"
    }
    $candidateBuildIdentity = if ($sourceIdentity) {
        Assert-InstalledBuildIdentity -Sidecar $sidecar -ExpectedVersion $version -RequireCurrentSource $true
    } else { $null }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $application.FullName
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['AGENT_DESKTOP_DATA_DIRECTORY'] = $dataDirectory
    $applicationProcess = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $applicationProcess) { throw 'Installed application did not start.' }

    $log = Join-Path $dataDirectory 'logs\agent.log'
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $applicationProcess.Refresh()
        if ($applicationProcess.HasExited) {
            throw "Installed application exited before becoming ready with code $($applicationProcess.ExitCode)."
        }
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($applicationProcess.Id)" -ErrorAction SilentlyContinue)
        $sidecarProcess = $children | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
        if ($sidecarProcess) { $sidecarProcessId = [int]$sidecarProcess.ProcessId }
        $ready = (Test-Path -LiteralPath $database) -and (Test-Path -LiteralPath $log) -and $null -ne $sidecarProcessId
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw 'Installed application did not create isolated data or start its backend within 45 seconds.' }

    if (-not $applicationProcess.CloseMainWindow()) { throw 'Installed application did not expose a closable main window.' }
    if (-not $applicationProcess.WaitForExit(15000)) { throw 'Installed application did not exit within 15 seconds.' }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $sidecarProcessId -ErrorAction SilentlyContinue) {
        throw "Backend sidecar process $sidecarProcessId remained after the desktop application exited."
    }
    $upgradeVerification = & $python $fixtureScript verify $database --schema $schemaVersion
    if ($LASTEXITCODE -ne 0) { throw 'Installed candidate did not migrate and preserve the upgrade fixture.' }

    $upgrade = Start-Process -FilePath $nsis.FullName -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($upgrade.ExitCode -ne 0) { throw "NSIS in-place upgrade failed with exit code $($upgrade.ExitCode)." }
    if (-not (Test-Path -LiteralPath $database) -or -not (Test-Path -LiteralPath $log)) {
        throw 'In-place upgrade removed isolated application data.'
    }

    $uninstaller = Get-ChildItem -LiteralPath $installDirectory -Recurse -Filter 'uninstall.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $uninstaller) { throw 'Installed NSIS uninstaller is missing.' }
    $uninstall = Start-Process -FilePath $uninstaller.FullName -ArgumentList '/S' -Wait -PassThru -WindowStyle Hidden
    if ($uninstall.ExitCode -ne 0) { throw "NSIS uninstall failed with exit code $($uninstall.ExitCode)." }
    $uninstalled = $true
    if (-not (Test-Path -LiteralPath $database)) { throw 'Uninstall removed the isolated application database.' }

    $reinstall = Start-Process -FilePath $nsis.FullName -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($reinstall.ExitCode -ne 0) { throw "NSIS reinstall failed with exit code $($reinstall.ExitCode)." }
    $uninstalled = $false
    $reinstalledApplication = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
        Select-Object -First 1
    if (-not $reinstalledApplication) { throw 'NSIS reinstall did not restore the desktop executable.' }
    if (-not (Test-Path -LiteralPath $database)) { throw 'NSIS reinstall could not see preserved private data.' }

    $restartInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $restartInfo.FileName = $reinstalledApplication.FullName
    $restartInfo.UseShellExecute = $false
    $restartInfo.CreateNoWindow = $true
    $restartInfo.EnvironmentVariables['AGENT_DESKTOP_DATA_DIRECTORY'] = $dataDirectory
    $applicationProcess = [System.Diagnostics.Process]::Start($restartInfo)
    if (-not $applicationProcess) { throw 'Reinstalled application did not start.' }
    $sidecarProcessId = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $applicationProcess.Refresh()
        if ($applicationProcess.HasExited) {
            throw "Reinstalled application exited before becoming ready with code $($applicationProcess.ExitCode)."
        }
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($applicationProcess.Id)" -ErrorAction SilentlyContinue)
        $sidecarProcess = $children | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
        if ($sidecarProcess) { $sidecarProcessId = [int]$sidecarProcess.ProcessId }
        $ready = (Test-Path -LiteralPath $database) -and $null -ne $sidecarProcessId
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw 'Reinstalled application did not recognize isolated data within 45 seconds.' }
    if (-not $applicationProcess.CloseMainWindow()) { throw 'Reinstalled application did not expose a closable main window.' }
    if (-not $applicationProcess.WaitForExit(15000)) { throw 'Reinstalled application did not exit within 15 seconds.' }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $sidecarProcessId -ErrorAction SilentlyContinue) {
        throw "Reinstalled backend sidecar process $sidecarProcessId remained after the desktop application exited."
    }
    $finalUninstaller = Get-ChildItem -LiteralPath $installDirectory -Recurse -Filter 'uninstall.exe' |
        Select-Object -First 1
    if (-not $finalUninstaller) { throw 'Reinstalled NSIS uninstaller is missing.' }
    $finalUninstall = Start-Process -FilePath $finalUninstaller.FullName -ArgumentList '/S' -Wait -PassThru -WindowStyle Hidden
    if ($finalUninstall.ExitCode -ne 0) { throw "Final NSIS uninstall failed with exit code $($finalUninstall.ExitCode)." }
    $uninstalled = $true
    $packageRemovalDeadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        $remainingPackageFile = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($remainingPackageFile) { Start-Sleep -Milliseconds 250 }
    } while ($remainingPackageFile -and [DateTime]::UtcNow -lt $packageRemovalDeadline)
    if ($remainingPackageFile) { throw 'Final NSIS uninstall left package files installed.' }

    $legacyPayload = [ordered]@{
        status = 'ok'
        recorded_at = [DateTime]::UtcNow.ToString('o')
        version = $version
        nsis = $nsis.Name
        msi = $msi.Name
        application_bytes = $application.Length
        sidecar_bytes = $sidecar.Length
        desktop_started = $true
        isolated_database = $true
        isolated_log = $true
        sidecar_stopped = $true
        schema_migrated = $true
        migration_backup = ($upgradeVerification | ConvertFrom-Json).migration_backup
        previous_version_upgrade = $previousVersionUpgrade
        in_place_upgrade_preserved_data = $true
        uninstall_preserved_data = $true
        reinstall_started = $true
        reinstall_recognized_data = $true
        final_uninstall = $true
        package_files_removed = $true
    }
    if ($sourceIdentity) {
        $requiredFacts = [ordered]@{
            candidate_version_matches_target = ([string]$candidateArtifact.version -eq $version)
            candidate_nsis_present = ($nsis.Length -ge 1MB)
            candidate_build_identity_matches_source = ($null -ne $candidateBuildIdentity)
            previous_installer_supplied = [bool]$PreviousInstaller
            previous_build_identity_matches_artifact = ($null -ne $previousBuildIdentity)
            previous_version_upgrade = $previousVersionUpgrade
            isolated_desktop_started = $true
            sidecar_stopped = $true
            schema_migrated = $true
            migration_backup_created = -not [string]::IsNullOrWhiteSpace([string]$legacyPayload.migration_backup)
            in_place_upgrade_preserved_data = $true
            uninstall_preserved_user_data = $true
            reinstall_started = $true
            reinstall_recognized_user_data = $true
            final_uninstall_completed = $true
            package_files_completely_removed = $true
        }
        if ($legacyEvidenceMode) {
            $requiredFacts['non_administrator_execution'] = (-not $isAdministrator)
        }
        $checks = [ordered]@{}
        foreach ($entry in $requiredFacts.GetEnumerator()) {
            $checks[$entry.Key] = [ordered]@{ passed = [bool]$entry.Value }
        }
        $allPassed = -not ($requiredFacts.Values -contains $false)
        $payload = [ordered]@{
            schema_version = 1
            report_type = if ($legacyEvidenceMode) { 'v14_nsis_installer_live_evidence' } else { 'release_nsis_installer_live_evidence' }
            producer = 'scripts/smoke-installer.ps1'
            target_version = $version
            status = if ($allPassed) { 'PASS' } else { 'FAIL' }
            actual_run = $true
            source = $sourceIdentity
            run = [ordered]@{
                run_id = $runId
                started_at = $startedAt
                finished_at = [DateTime]::UtcNow.ToString('o')
                installer_kind = 'NSIS'
                elevation = if ($isAdministrator) { 'ADMINISTRATOR' } else { 'NON_ADMINISTRATOR' }
                isolated_test_data = $true
            }
            artifacts = [ordered]@{
                candidate = $candidateArtifact
                previous = $previousArtifact
                previous_build_manifest = $previousBuildIdentity
                build_manifest = $candidateBuildIdentity
            }
            checks = $checks
            results = $legacyPayload
        }
        $json = $payload | ConvertTo-Json -Depth 12
        Write-JsonResult -Json $json -Destination $Output -Immutable $true
    } else {
        $json = $legacyPayload | ConvertTo-Json -Depth 6
        Write-JsonResult -Json $json -Destination $Output -Immutable $false
    }
    Write-Output $json
} finally {
    if ($applicationProcess -and -not $applicationProcess.HasExited) {
        & taskkill.exe /PID $applicationProcess.Id /T /F 2>$null | Out-Null
    } elseif ($sidecarProcessId -and (Get-Process -Id $sidecarProcessId -ErrorAction SilentlyContinue)) {
        Stop-Process -Id $sidecarProcessId -Force -ErrorAction SilentlyContinue
    }
    $uninstaller = Get-ChildItem -LiteralPath $installDirectory -Recurse -Filter 'uninstall.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $uninstalled -and $uninstaller) {
        Start-Process -FilePath $uninstaller.FullName -ArgumentList '/S' -Wait -WindowStyle Hidden | Out-Null
    }
    foreach ($temporaryPath in @($installDirectory, $dataDirectory)) {
        if (-not (Test-Path -LiteralPath $temporaryPath)) { continue }
        $resolvedInstall = [System.IO.Path]::GetFullPath($temporaryPath)
        $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP)
        $temporaryBoundary = $resolvedTemp.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
        $candidateBoundary = $resolvedInstall.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $candidateBoundary.StartsWith($temporaryBoundary, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean installer smoke path outside TEMP: $resolvedInstall"
        }
        Remove-Item -LiteralPath $resolvedInstall -Recurse -Force
    }
    Remove-TestOwnedInstallRegistry -ExpectedDirectory $installDirectory
}
