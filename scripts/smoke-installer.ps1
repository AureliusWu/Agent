param(
    [string]$BundleDirectory = '',
    [string]$PreviousInstaller = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
$fixtureScript = Join-Path $root 'scripts\upgrade-database-fixture.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Backend Python environment is required for the upgrade fixture.' }
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $root 'frontend\src-tauri\target\release\bundle'
}
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$nsis = Get-ChildItem -LiteralPath (Join-Path $bundle 'nsis') -Filter "Agent_${version}_*-setup.exe" | Select-Object -First 1
$msi = Get-ChildItem -LiteralPath (Join-Path $bundle 'msi') -Filter "Agent_${version}_*.msi" | Select-Object -First 1
if (-not $nsis -or -not $msi) { throw 'Expected both NSIS and MSI installers.' }
if ($nsis.Length -lt 1MB -or $msi.Length -lt 1MB) { throw 'Installer output is unexpectedly small.' }

$installDirectory = Join-Path $env:TEMP ('agent-installer-smoke-' + [Guid]::NewGuid().ToString('N'))
$dataDirectory = Join-Path $env:TEMP ('agent-data-smoke-' + [Guid]::NewGuid().ToString('N'))
$applicationProcess = $null
$sidecarProcessId = $null
$uninstalled = $false
try {
    $installArguments = @('/S', "/D=$installDirectory")
    $initialInstaller = $nsis.FullName
    if ($PreviousInstaller) {
        $initialInstaller = (Resolve-Path -LiteralPath $PreviousInstaller).Path
        if ([System.IO.Path]::GetExtension($initialInstaller) -ine '.exe') { throw 'Previous installer must be an NSIS executable.' }
    }
    $install = Start-Process -FilePath $initialInstaller -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) { throw "NSIS installation failed with exit code $($install.ExitCode)." }

    New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
    $database = Join-Path $dataDirectory 'agent.db'
    & $python $fixtureScript create $database --schema 14 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the schema 14 upgrade fixture.' }

    $previousVersionUpgrade = $false
    if ($PreviousInstaller) {
        $candidateInstall = Start-Process -FilePath $nsis.FullName -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
        if ($candidateInstall.ExitCode -ne 0) { throw "Candidate upgrade failed with exit code $($candidateInstall.ExitCode)." }
        $previousVersionUpgrade = $true
    }
    $executables = @(Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter '*.exe')
    $application = $executables | Where-Object { $_.Name -ieq 'Agent.exe' } | Select-Object -First 1
    $sidecar = $executables | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
    if (-not $application -or -not $sidecar) {
        throw "Installed application or backend sidecar is missing. Found: $($executables.Name -join ', ')"
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $application.FullName
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['AGENT_DESKTOP_DATA_DIRECTORY'] = $dataDirectory
    $applicationProcess = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $applicationProcess) { throw 'Installed application did not start.' }

    $log = Join-Path $dataDirectory 'logs\agent.log'
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
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
    if (-not $ready) { throw 'Installed application did not create isolated data or start its backend within 25 seconds.' }

    if (-not $applicationProcess.CloseMainWindow()) { throw 'Installed application did not expose a closable main window.' }
    if (-not $applicationProcess.WaitForExit(15000)) { throw 'Installed application did not exit within 15 seconds.' }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id $sidecarProcessId -ErrorAction SilentlyContinue) {
        throw "Backend sidecar process $sidecarProcessId remained after the desktop application exited."
    }
    $upgradeVerification = & $python $fixtureScript verify $database --schema 15
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

    [pscustomobject]@{
        status = 'ok'
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
    } | ConvertTo-Json
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
}
