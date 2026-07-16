$vsWhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vsWhere)) {
    throw 'Visual Studio installer discovery tool is missing.'
}

$vsInstall = (& $vsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1)
if (-not $vsInstall) {
    throw 'Visual Studio C++ Build Tools are missing.'
}

$vsDevCmd = Join-Path $vsInstall 'Common7\Tools\VsDevCmd.bat'
if (-not (Test-Path -LiteralPath $vsDevCmd)) {
    throw "Visual Studio developer environment is missing at $vsDevCmd."
}

$vsEnvironment = & cmd.exe /d /s /c "call `"$vsDevCmd`" -arch=x64 >nul && set"
if ($LASTEXITCODE -ne 0) {
    throw "Visual Studio environment initialization failed with exit code $LASTEXITCODE."
}

foreach ($line in $vsEnvironment) {
    $separator = $line.IndexOf('=')
    if ($separator -le 0) { continue }
    [Environment]::SetEnvironmentVariable($line.Substring(0, $separator), $line.Substring($separator + 1), 'Process')
}

$env:PATH = (Join-Path $env:USERPROFILE '.cargo\bin') + ';' + $env:PATH
$linker = (Get-Command link.exe -CommandType Application -ErrorAction Stop).Source
if (-not $linker.StartsWith($vsInstall, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Expected the Visual Studio linker, but resolved $linker."
}
