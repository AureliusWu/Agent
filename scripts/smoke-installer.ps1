param(
    [string]$BundleDirectory = ''
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$version = (Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim()
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $root 'frontend\src-tauri\target\release\bundle'
}
$bundle = (Resolve-Path -LiteralPath $BundleDirectory).Path
$nsis = Get-ChildItem -LiteralPath (Join-Path $bundle 'nsis') -Filter "Agent_${version}_*-setup.exe" | Select-Object -First 1
$msi = Get-ChildItem -LiteralPath (Join-Path $bundle 'msi') -Filter "Agent_${version}_*.msi" | Select-Object -First 1
if (-not $nsis -or -not $msi) { throw 'Expected both NSIS and MSI installers.' }
if ($nsis.Length -lt 1MB -or $msi.Length -lt 1MB) { throw 'Installer output is unexpectedly small.' }

$installDirectory = Join-Path $env:TEMP ('agent-installer-smoke-' + [Guid]::NewGuid().ToString('N'))
try {
    $installArguments = @('/S', "/D=$installDirectory")
    $install = Start-Process -FilePath $nsis.FullName -ArgumentList $installArguments -Wait -PassThru -WindowStyle Hidden
    if ($install.ExitCode -ne 0) { throw "NSIS installation failed with exit code $($install.ExitCode)." }
    $executables = @(Get-ChildItem -LiteralPath $installDirectory -Recurse -File -Filter '*.exe')
    $application = $executables | Where-Object { $_.Name -ieq 'Agent.exe' } | Select-Object -First 1
    $sidecar = $executables | Where-Object { $_.Name -like 'agent-backend*.exe' } | Select-Object -First 1
    if (-not $application -or -not $sidecar) {
        throw "Installed application or backend sidecar is missing. Found: $($executables.Name -join ', ')"
    }
    [pscustomobject]@{
        status = 'ok'
        nsis = $nsis.Name
        msi = $msi.Name
        application_bytes = $application.Length
        sidecar_bytes = $sidecar.Length
    } | ConvertTo-Json
} finally {
    $uninstaller = Get-ChildItem -LiteralPath $installDirectory -Recurse -Filter 'uninstall.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($uninstaller) {
        Start-Process -FilePath $uninstaller.FullName -ArgumentList '/S' -Wait -WindowStyle Hidden | Out-Null
    }
    if (Test-Path -LiteralPath $installDirectory) {
        $resolvedInstall = [System.IO.Path]::GetFullPath($installDirectory)
        $resolvedTemp = [System.IO.Path]::GetFullPath($env:TEMP)
        if (-not $resolvedInstall.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean installer smoke path outside TEMP: $resolvedInstall"
        }
        Remove-Item -LiteralPath $resolvedInstall -Recurse -Force
    }
}
