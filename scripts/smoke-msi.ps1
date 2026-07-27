param(
    [string]$BundleDirectory = '',
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
$tauriConfig = Get-Content -LiteralPath (Join-Path $root 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding utf8 | ConvertFrom-Json
$productName = [string]$tauriConfig.productName
$applicationName = "{0}.exe" -f [string]$tauriConfig.mainBinaryName
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
$uninstallLog = Join-Path $env:TEMP ('siyi-msi-uninstall-' + [Guid]::NewGuid().ToString('N') + '.log')
$applicationProcess = $null
$installed = $false

function Invoke-Msi {
    param([string[]]$Arguments)
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
        throw "msiexec failed with exit code $($process.ExitCode)."
    }
}

function Start-IsolatedApplication {
    param([string]$Executable)
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Executable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables['AGENT_DESKTOP_DATA_DIRECTORY'] = $dataDirectory
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if (-not $process) { throw 'MSI-installed application did not start.' }
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
    if (-not $process.CloseMainWindow()) { throw 'MSI-installed application has no closable main window.' }
    if (-not $process.WaitForExit(15000)) { throw 'MSI-installed application did not close within 15 seconds.' }
    Start-Sleep -Milliseconds 500
    if (Get-Process -Id ([int]$sidecar.ProcessId) -ErrorAction SilentlyContinue) {
        throw 'MSI-installed application left its sidecar running.'
    }
    return $database
}

try {
    New-Item -ItemType Directory -Force -Path $installDirectory, $dataDirectory | Out-Null
    Invoke-Msi @('/i', "`"$($msi.FullName)`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$installLog`"")
    $installed = $true
    $application = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
        Select-Object -First 1
    if (-not $application) { throw 'MSI installation did not produce the desktop executable.' }
    $applicationProcess = $null
    $database = Start-IsolatedApplication $application.FullName
    $hashBeforeUninstall = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash

    Invoke-Msi @('/x', "`"$($msi.FullName)`"", '/qn', '/norestart', '/L*v', "`"$uninstallLog`"")
    $installed = $false
    if (-not (Test-Path -LiteralPath $database)) { throw 'MSI uninstall removed isolated private data.' }
    $hashAfterUninstall = (Get-FileHash -LiteralPath $database -Algorithm SHA256).Hash
    if ($hashAfterUninstall -ne $hashBeforeUninstall) { throw 'MSI uninstall changed isolated private data.' }

    Invoke-Msi @('/i', "`"$($msi.FullName)`"", '/qn', '/norestart', "INSTALLDIR=`"$installDirectory`"", '/L*v', "`"$installLog`"")
    $installed = $true
    $reinstalledApplication = Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter $applicationName |
        Select-Object -First 1
    if (-not $reinstalledApplication) { throw 'MSI reinstall did not restore the desktop executable.' }
    if (-not (Test-Path -LiteralPath $database)) { throw 'MSI reinstall could not see preserved private data.' }

    $payload = [ordered]@{
        status = 'ok'
        recorded_at = [DateTime]::UtcNow.ToString('o')
        version = $version
        package = $msi.Name
        install = $true
        desktop_started = $true
        sidecar_stopped = $true
        uninstall = $true
        uninstall_preserved_data = $true
        reinstall = $true
        reinstall_recognized_data = $true
    }
    $json = $payload | ConvertTo-Json
    if ($Output) {
        $outputPath = [System.IO.Path]::GetFullPath((Join-Path $root $Output))
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
        Set-Content -LiteralPath $outputPath -Value $json -Encoding utf8
    }
    Write-Output $json
} catch {
    $isPrivilegeBlock = (Test-Path -LiteralPath $installLog) -and
        (Select-String -LiteralPath $installLog -Pattern '1925' -Quiet)
    $failurePayload = [ordered]@{
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
    $failureJson = $failurePayload | ConvertTo-Json
    if ($Output) {
        $outputPath = [System.IO.Path]::GetFullPath((Join-Path $root $Output))
        $outputDirectory = Split-Path -Parent $outputPath
        New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
        Set-Content -LiteralPath $outputPath -Value $failureJson -Encoding utf8
        if (Test-Path -LiteralPath $installLog) {
            Copy-Item -LiteralPath $installLog -Destination (Join-Path $outputDirectory 'msi-install.log') -Force
        }
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
    Remove-Item -LiteralPath $installLog, $uninstallLog -Force -ErrorAction SilentlyContinue
}
