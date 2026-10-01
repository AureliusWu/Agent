param(
    [string]$BundleDirectory = '',
    [string]$PreviousInstaller = '',
    [switch]$InteractiveAcceptance,
    [string]$Operator = '',
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$tauriConfig = Get-Content -LiteralPath (Join-Path $root 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding utf8 | ConvertFrom-Json
$productName = [string]$tauriConfig.productName
$applicationName = "{0}.exe" -f [string]$tauriConfig.mainBinaryName
$testInstallRegistry = "HKCU:\Software\github\$productName"
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$fixtureScript = Join-Path $root 'scripts\upgrade-database-fixture.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Backend Python environment is required for the MSI upgrade fixture.' }
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
$msi = Get-ChildItem -LiteralPath (Join-Path $bundle 'msi') -Filter "${productName}_${version}_*.msi" |
    Select-Object -First 1
if (-not $msi) { throw 'Expected MSI installer was not found.' }

$installDirectory = Join-Path $env:TEMP ('siyi-msi-smoke-' + [Guid]::NewGuid().ToString('N'))
$dataDirectory = Join-Path $env:TEMP ('siyi-msi-data-' + [Guid]::NewGuid().ToString('N'))
$installLog = Join-Path $env:TEMP ('siyi-msi-install-' + [Guid]::NewGuid().ToString('N') + '.log')
$upgradeLog = Join-Path $env:TEMP ('siyi-msi-upgrade-' + [Guid]::NewGuid().ToString('N') + '.log')
$uninstallLog = Join-Path $env:TEMP ('siyi-msi-uninstall-' + [Guid]::NewGuid().ToString('N') + '.log')
$applicationProcess = $null
$installed = $false
$runId = [Guid]::NewGuid().ToString('N')
$startedAt = [DateTime]::UtcNow.ToString('o')
$operatorAcceptance = $null
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
    if (-not $Output) { throw 'A runner-bound MSI evidence run requires -Output.' }
}
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($currentIdentity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Get-InstallerArtifactIdentity {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$File,
        [bool]$AllowCanonicalReleaseName = $false
    )
    if (-not $File.Name.EndsWith('.msi', [System.StringComparison]::OrdinalIgnoreCase)) {
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
        [Parameter(Mandatory = $true)][System.IO.FileInfo]$Application,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [bool]$RequireCurrentSource
    )
    $manifestPath = Join-Path $Application.DirectoryName '_internal\build-info.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Installed MSI candidate does not contain its embedded build manifest.'
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
        throw 'Installed MSI package build identity does not match its installer artifact version.'
    }
    if (
        $RequireCurrentSource -and
        (
            $manifest.git_commit -ne $sourceIdentity.source_commit -or
            $manifest.source_fingerprint -ne $sourceIdentity.source_tree_fingerprint -or
            $manifest.workspace_state -ne 'CLEAN'
        )
    ) {
        throw 'Installed MSI candidate build identity does not match the runner-bound clean source.'
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

$candidateArtifact = Get-InstallerArtifactIdentity -File $msi
$initialInstaller = $msi.FullName
$previousArtifact = $null
if ($PreviousInstaller) {
    $initialInstaller = (Resolve-Path -LiteralPath $PreviousInstaller).Path
    $previousFile = Get-Item -LiteralPath $initialInstaller
    $previousArtifact = Get-InstallerArtifactIdentity -File $previousFile -AllowCanonicalReleaseName $true
}
if ($sourceIdentity) {
    if ($sourceIdentity.source_version -ne $version) {
        throw "Runner-bound MSI acceptance requires synchronized $version source and package metadata."
    }
    if ($sourceIdentity.workspace_clean -ne $true) {
        throw 'Runner-bound MSI acceptance requires a clean source workspace.'
    }
    if (-not $isAdministrator) {
        throw 'A28 MSI acceptance requires an administrator process.'
    }
    if ($legacyEvidenceMode -and (
        -not $InteractiveAcceptance -or
        -not [Environment]::UserInteractive -or
        $Host.Name -ne 'ConsoleHost' -or
        [string]::IsNullOrWhiteSpace($Operator)
    )) {
        throw 'A28 MSI acceptance requires -InteractiveAcceptance, a ConsoleHost, and -Operator.'
    }
    if ($InteractiveAcceptance -and (
        -not [Environment]::UserInteractive -or
        $Host.Name -ne 'ConsoleHost' -or
        [string]::IsNullOrWhiteSpace($Operator)
    )) {
        throw 'Interactive MSI acceptance requires a ConsoleHost and a non-empty -Operator identifier.'
    }
    if (-not $previousArtifact) {
        throw 'A28 MSI acceptance requires -PreviousInstaller for a real prior-version upgrade.'
    }
    if (
        [version]$previousArtifact.version -ge [version]$candidateArtifact.version -or
        $previousArtifact.sha256 -eq $candidateArtifact.sha256
    ) {
        throw 'A28 previous MSI installer must be a distinct version older than the current candidate.'
    }
}

function Remove-StaleTestInstallRegistry {
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
    $tempBoundary = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    $leaf = Split-Path -Leaf $resolved
    if (
        ($resolved + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
            $tempBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        ($leaf -like 'agent-installer-smoke-*' -or $leaf -like 'siyi-msi-smoke-*')
    ) {
        Remove-Item -LiteralPath $testInstallRegistry -Recurse -Force
    }
}

function Resolve-EvidenceOutputPath {
    param([Parameter(Mandatory = $true)][string]$Destination)
    $outputPath = if ([System.IO.Path]::IsPathRooted($Destination)) {
        [System.IO.Path]::GetFullPath($Destination)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $root $Destination))
    }
    if ($sourceIdentity) {
        $evidenceRoot = [System.IO.Path]::GetFullPath((& (Join-Path $PSScriptRoot 'evidence-root.ps1') -RepositoryRoot $root -EvidenceVersion $version))
        $evidenceBoundary = $evidenceRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not ($outputPath + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
            $evidenceBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Runner-bound MSI evidence must stay under the v$version evidence root."
        }
    }
    return $outputPath
}

function Write-JsonResult {
    param(
        [Parameter(Mandatory = $true)][string]$Json,
        [string]$Destination,
        [bool]$Immutable
    )
    if (-not $Destination) { return }
    $outputPath = Resolve-EvidenceOutputPath -Destination $Destination
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
            throw "Refusing to overwrite immutable MSI evidence: $outputPath"
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

function Copy-EvidenceLog {
    param([string]$LogPath, [string]$Label)
    if (-not $Output -or -not (Test-Path -LiteralPath $LogPath)) { return $null }
    $outputPath = Resolve-EvidenceOutputPath -Destination $Output
    $destination = Join-Path (Split-Path -Parent $outputPath) ("{0}-{1}.log" -f $Label, $runId)
    if (Test-Path -LiteralPath $destination) {
        throw "Refusing to overwrite immutable MSI log evidence: $destination"
    }
    [System.IO.File]::Copy($LogPath, $destination, $false)
    return [ordered]@{
        path = [System.IO.Path]::GetRelativePath($root, $destination).Replace('\', '/')
        sha256 = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
        bytes = (Get-Item -LiteralPath $destination).Length
    }
}

function Read-OperatorConfirmation {
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$Instruction)
    $challenge = [Guid]::NewGuid().ToString('N').Substring(0, 10).ToUpperInvariant()
    Write-Host "[A28/$Name] $Instruction"
    $response = Read-Host "Observation complete? Type PASS-$challenge"
    $confirmed = $response -ceq "PASS-$challenge"
    return [ordered]@{
        name = $Name
        confirmed = $confirmed
        confirmed_at = [DateTime]::UtcNow.ToString('o')
        challenge_sha256 = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($challenge))
        )
    }
}

function Read-MsiDesktopAcceptance {
    if (-not $InteractiveAcceptance) { return $null }
    if (-not [Environment]::UserInteractive -or $Host.Name -ne 'ConsoleHost') {
        throw 'MSI desktop acceptance requires an interactive ConsoleHost.'
    }
    if ([string]::IsNullOrWhiteSpace($Operator)) {
        throw 'MSI desktop acceptance requires a non-empty -Operator identifier.'
    }
    $items = @(
        Read-OperatorConfirmation -Name 'microphone_permission_grant' -Instruction 'Use the installed app to grant microphone permission and verify capture becomes available.'
        Read-OperatorConfirmation -Name 'microphone_permission_denial' -Instruction 'Deny microphone permission and verify the installed app fails closed with a visible error, then restore permission.'
        Read-OperatorConfirmation -Name 'local_stt_transcription' -Instruction 'Record a short Chinese sentence and verify local STT returns an editable transcript.'
        Read-OperatorConfirmation -Name 'windows_tts_playback' -Instruction 'Play one response through Windows TTS and verify audible local playback plus stop.'
        Read-OperatorConfirmation -Name 'ollama_detection' -Instruction 'Verify the installed app reports the real local Ollama service and installed model state.'
    )
    return [ordered]@{
        interactive = $true
        operator = $Operator.Trim()
        observations = $items
        all_confirmed = -not ($items.confirmed -contains $false)
    }
}

function Invoke-Msi {
    param([string[]]$Arguments)
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "msiexec failed with exit code $($process.ExitCode)."
    }
}

function Start-IsolatedApplication {
    param([string]$Executable, [bool]$CollectOperatorAcceptance = $false)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['AGENT_DESKTOP_DATA_DIRECTORY'] = $dataDirectory
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $process) { throw 'MSI-installed application did not start.' }
    $script:applicationProcess = $process
    $database = Join-Path $dataDirectory 'data\agent.db'
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        $process.Refresh()
        if ($process.HasExited) {
            throw "MSI-installed application exited before readiness with code $($process.ExitCode)."
        }
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($process.Id)" -ErrorAction SilentlyContinue)
        $sidecar = $children | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
        $ready = (Test-Path -LiteralPath $database) -and $null -ne $sidecar
    } while (-not $ready -and [DateTime]::UtcNow -lt $deadline)
    if (-not $ready) { throw 'MSI-installed application did not become ready within 45 seconds.' }
    $acceptance = if ($CollectOperatorAcceptance) { Read-MsiDesktopAcceptance } else { $null }
    if (-not $process.CloseMainWindow()) { throw 'MSI-installed application has no closable main window.' }
    if (-not $process.WaitForExit(15000)) { throw 'MSI-installed application did not close within 15 seconds.' }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id ([int]$sidecar.ProcessId) -ErrorAction SilentlyContinue) {
        throw 'MSI-installed application left its sidecar running.'
    }
    $script:applicationProcess = $null
    return [ordered]@{
        database = $database
        operator_acceptance = $acceptance
    }
}

try {
    Remove-StaleTestInstallRegistry
    New-Item -ItemType Directory -Force -Path $installDirectory, $dataDirectory | Out-Null
    Invoke-Msi @('/i', "`"$initialInstaller`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$installLog`"")
    $installed = $true
    $previousBuildIdentity = $null
    if ($sourceIdentity) {
        $previousApplication = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
            Select-Object -First 1
        if (-not $previousApplication) { throw 'Previous MSI installation did not produce the desktop executable.' }
        $previousBuildIdentity = Assert-InstalledBuildIdentity `
            -Application $previousApplication `
            -ExpectedVersion ([string]$previousArtifact.version) `
            -RequireCurrentSource $false
    }
    $database = Join-Path $dataDirectory 'data\agent.db'
    & $python $fixtureScript create $database --schema $fixtureSchemaVersion | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the schema $fixtureSchemaVersion MSI upgrade fixture." }
    $modelSentinel = Join-Path $dataDirectory 'voice\models\installer-smoke-model\sentinel.txt'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $modelSentinel) | Out-Null
    Set-Content -LiteralPath $modelSentinel -Value "v$version-msi-model-$runId" -Encoding utf8
    $modelHashBefore = (Get-FileHash -LiteralPath $modelSentinel -Algorithm SHA256).Hash

    $previousVersionUpgrade = $false
    if ($PreviousInstaller) {
        Invoke-Msi @('/i', "`"$($msi.FullName)`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$upgradeLog`"")
        $previousVersionUpgrade = $true
    }
    $application = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
        Select-Object -First 1
    if (-not $application) { throw 'MSI installation did not produce the desktop executable.' }
    $candidateBuildIdentity = if ($sourceIdentity) {
        Assert-InstalledBuildIdentity -Application $application -ExpectedVersion $version -RequireCurrentSource $true
    } else { $null }
    $applicationProcess = $null
    $installedRuntimeIdentity = $null
    if ($sourceIdentity) {
        $runtimeJson = & $python (Join-Path $root 'scripts\read-installed-runtime.py') --directory $application.DirectoryName --desktop $application.Name --capture-source
        if ($LASTEXITCODE -ne 0) { throw 'Actual MSI-installed executable/payload capture failed.' }
        $installedRuntimeIdentity = $runtimeJson | ConvertFrom-Json
    }
    $launch = Start-IsolatedApplication -Executable $application.FullName -CollectOperatorAcceptance ([bool]$InteractiveAcceptance)
    $database = [string]$launch.database
    $operatorAcceptance = $launch.operator_acceptance
    $upgradeVerification = & $python $fixtureScript verify $database --schema $schemaVersion
    if ($LASTEXITCODE -ne 0) { throw 'MSI-installed candidate did not migrate and preserve the upgrade fixture.' }
    $hashBeforeUninstall = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash

    Invoke-Msi @('/i', "`"$($msi.FullName)`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$upgradeLog`"")
    if (-not (Test-Path -LiteralPath $database) -or -not (Test-Path -LiteralPath $modelSentinel)) {
        throw 'MSI in-place upgrade removed isolated user data or the model sentinel.'
    }

    Invoke-Msi @('/x', "`"$($msi.FullName)`"", '/qn', '/norestart', '/L*v', "`"$uninstallLog`"")
    $installed = $false
    if (-not (Test-Path -LiteralPath $database)) { throw 'MSI uninstall removed isolated private data.' }
    $hashAfterUninstall = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash
    if ($hashAfterUninstall -ne $hashBeforeUninstall) { throw 'MSI uninstall changed isolated private data.' }
    if (-not (Test-Path -LiteralPath $modelSentinel) -or
        (Get-FileHash -LiteralPath $modelSentinel -Algorithm SHA256).Hash -ne $modelHashBefore) {
        throw 'MSI uninstall removed or changed the isolated model sentinel.'
    }

    Invoke-Msi @('/i', "`"$($msi.FullName)`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$installLog`"")
    $installed = $true
    $reinstalledApplication = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
        Select-Object -First 1
    if (-not $reinstalledApplication) { throw 'MSI reinstall did not restore the desktop executable.' }
    if (-not (Test-Path -LiteralPath $database)) { throw 'MSI reinstall could not see preserved private data.' }
    $reinstallLaunch = Start-IsolatedApplication -Executable $reinstalledApplication.FullName
    if (-not (Test-Path -LiteralPath ([string]$reinstallLaunch.database))) {
        throw 'MSI reinstall did not recognize the preserved database.'
    }
    Invoke-Msi @('/x', "`"$($msi.FullName)`"", '/qn', '/norestart', '/L*v', "`"$uninstallLog`"")
    $installed = $false
    $installedApplicationRemains = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($installedApplicationRemains) { throw 'Final MSI uninstall left package files installed.' }
    if (-not (Test-Path -LiteralPath $database) -or -not (Test-Path -LiteralPath $modelSentinel)) {
        throw 'Final MSI uninstall removed isolated user data or models.'
    }

    $legacyPayload = [ordered]@{
        status = 'ok'
        recorded_at = [DateTime]::UtcNow.ToString('o')
        version = $version
        package = $msi.Name
        install = $true
        desktop_started = $true
        sidecar_stopped = $true
        previous_version_upgrade = $previousVersionUpgrade
        schema_migrated = $true
        migration_backup = ($upgradeVerification | ConvertFrom-Json).migration_backup
        in_place_upgrade_preserved_data = $true
        uninstall = $true
        uninstall_preserved_data = $true
        uninstall_preserved_models = $true
        reinstall = $true
        reinstall_recognized_data = $true
        final_uninstall = $true
        package_files_removed = $true
    }
    if ($sourceIdentity) {
        $observationMap = @{}
        if ($operatorAcceptance -and $operatorAcceptance.observations) {
            foreach ($observation in $operatorAcceptance.observations) {
                $observationMap[[string]$observation.name] = [bool]$observation.confirmed
            }
        }
        $requiredFacts = [ordered]@{
            administrator_execution = $isAdministrator
            candidate_version_matches_target = ([string]$candidateArtifact.version -eq $version)
            candidate_msi_present = ($msi.Length -ge 1MB)
            candidate_build_identity_matches_source = ($null -ne $candidateBuildIdentity)
            previous_installer_supplied = [bool]$PreviousInstaller
            previous_build_identity_matches_artifact = ($null -ne $previousBuildIdentity)
            fresh_install = $true
            installed_desktop_started = $true
            previous_version_upgrade = $previousVersionUpgrade
            schema_migrated = $true
            migration_backup_created = -not [string]::IsNullOrWhiteSpace([string]$legacyPayload.migration_backup)
            uninstall_preserved_user_data = $true
            uninstall_preserved_models = $true
            package_files_completely_removed = $true
            reinstall_started = $true
            reinstall_recognized_user_data = $true
            final_uninstall_completed = $true
        }
        if ($legacyEvidenceMode -or $InteractiveAcceptance) {
            $requiredFacts['microphone_permission_grant'] = [bool]$observationMap['microphone_permission_grant']
            $requiredFacts['microphone_permission_denial'] = [bool]$observationMap['microphone_permission_denial']
            $requiredFacts['local_stt_transcription'] = [bool]$observationMap['local_stt_transcription']
            $requiredFacts['windows_tts_playback'] = [bool]$observationMap['windows_tts_playback']
            $requiredFacts['ollama_detection'] = [bool]$observationMap['ollama_detection']
        }
        $checks = [ordered]@{}
        foreach ($entry in $requiredFacts.GetEnumerator()) {
            $checks[$entry.Key] = [ordered]@{ passed = [bool]$entry.Value }
        }
        $allPassed = -not ($requiredFacts.Values -contains $false)
        $finalSourceJson = & $python (Join-Path $root 'scripts\read-installed-runtime.py') --source-only --build-id $candidateBuildIdentity.build_id
        if ($LASTEXITCODE -ne 0) { throw 'Could not capture source identity after the complete MSI lifecycle.' }
        $installedRuntimeIdentity.source_after = $finalSourceJson | ConvertFrom-Json
        $payload = [ordered]@{
            schema_version = 1
            report_type = if ($legacyEvidenceMode) { 'v14_msi_installer_live_evidence' } else { 'release_msi_installer_live_evidence' }
            producer = 'scripts/smoke-msi.ps1'
            target_version = $version
            status = if ($allPassed) { 'PASS' } else { 'FAIL' }
            actual_run = $true
            source = $sourceIdentity
            source_after = $installedRuntimeIdentity.source_after
            binary_sha256 = $installedRuntimeIdentity.binary_sha256
            sidecar_payload_sha256 = $installedRuntimeIdentity.sidecar_payload_sha256
            installed_sidecar_payload = $installedRuntimeIdentity.installed_sidecar_payload
            run = [ordered]@{
                run_id = $runId
                started_at = $startedAt
                finished_at = [DateTime]::UtcNow.ToString('o')
                installer_kind = 'MSI'
                elevation = if ($isAdministrator) { 'ADMINISTRATOR' } else { 'NON_ADMINISTRATOR' }
                isolated_test_data = $true
            }
            artifacts = [ordered]@{
                candidate = $candidateArtifact
                previous = $previousArtifact
                previous_build_manifest = $previousBuildIdentity
                build_manifest = $candidateBuildIdentity
            }
            logs = @(
                Copy-EvidenceLog -LogPath $installLog -Label 'msi-install'
                Copy-EvidenceLog -LogPath $upgradeLog -Label 'msi-upgrade'
                Copy-EvidenceLog -LogPath $uninstallLog -Label 'msi-uninstall'
            ) | Where-Object { $null -ne $_ }
            operator_acceptance = $operatorAcceptance
            checks = $checks
            results = $legacyPayload
        }
        $json = $payload | ConvertTo-Json -Depth 14
        Write-JsonResult -Json $json -Destination $Output -Immutable $true
    } else {
        $json = $legacyPayload | ConvertTo-Json -Depth 6
        Write-JsonResult -Json $json -Destination $Output -Immutable $false
    }
    Write-Output $json
} catch {
    $isPrivilegeBlock = (Test-Path -LiteralPath $installLog) -and
        (Select-String -LiteralPath $installLog -Pattern '1925' -Quiet)
    $legacyFailurePayload = [ordered]@{
        status = if ($isPrivilegeBlock) { 'blocked' } else { 'failed' }
        recorded_at = [DateTime]::UtcNow.ToString('o')
        version = $version
        package = $msi.Name
        reason = if ($isPrivilegeBlock) {
            'MSI is per-machine and the test process has no administrator privilege (Windows Installer error 1925).'
        } else {
            $_.Exception.Message
        }
    }
    if ($sourceIdentity) {
        $failurePayload = [ordered]@{
            schema_version = 1
            report_type = if ($legacyEvidenceMode) { 'v14_msi_installer_live_evidence' } else { 'release_msi_installer_live_evidence' }
            producer = 'scripts/smoke-msi.ps1'
            target_version = $version
            status = 'FAIL'
            actual_run = $true
            source = $sourceIdentity
            blocker = if (-not $isAdministrator -or $isPrivilegeBlock) { 'MSI_ADMINISTRATOR_REQUIRED' } else { 'MSI_LIFECYCLE_FAILED' }
            run = [ordered]@{
                run_id = $runId
                started_at = $startedAt
                finished_at = [DateTime]::UtcNow.ToString('o')
                installer_kind = 'MSI'
                elevation = if ($isAdministrator) { 'ADMINISTRATOR' } else { 'NON_ADMINISTRATOR' }
                isolated_test_data = $true
            }
            checks = [ordered]@{
                administrator_execution = [ordered]@{ passed = $isAdministrator }
            }
            reason = $legacyFailurePayload.reason
            logs = @(
                Copy-EvidenceLog -LogPath $installLog -Label 'msi-install'
                Copy-EvidenceLog -LogPath $upgradeLog -Label 'msi-upgrade'
                Copy-EvidenceLog -LogPath $uninstallLog -Label 'msi-uninstall'
            ) | Where-Object { $null -ne $_ }
        }
        $failureJson = $failurePayload | ConvertTo-Json -Depth 12
        Write-JsonResult -Json $failureJson -Destination $Output -Immutable $true
    } else {
        $failureJson = $legacyFailurePayload | ConvertTo-Json -Depth 5
        Write-JsonResult -Json $failureJson -Destination $Output -Immutable $false
    }
    Write-Output $failureJson
    throw
} finally {
    if ($applicationProcess -and -not $applicationProcess.HasExited) {
        & taskkill.exe /PID $applicationProcess.Id /T /F 2>$null | Out-Null
    }
    if ($installed) {
        try {
            Invoke-Msi @('/x', "`"$($msi.FullName)`"", '/qn', '/norestart')
        } catch {
            Write-Warning "MSI cleanup failed: $($_.Exception.Message)"
        }
    }
    foreach ($temporaryPath in @($installDirectory, $dataDirectory)) {
        if (-not (Test-Path -LiteralPath $temporaryPath)) { continue }
        $resolved = [System.IO.Path]::GetFullPath($temporaryPath)
        $tempBoundary = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
            [System.IO.Path]::DirectorySeparatorChar
        if (-not ($resolved + [System.IO.Path]::DirectorySeparatorChar).StartsWith(
            $tempBoundary,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to clean MSI smoke path outside TEMP: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    Remove-StaleTestInstallRegistry
    Remove-Item -LiteralPath $installLog, $upgradeLog, $uninstallLog -Force -ErrorAction SilentlyContinue
}
