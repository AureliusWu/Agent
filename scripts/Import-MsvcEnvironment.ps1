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
    $name = $line.Substring(0, $separator)
    if ($name.Equals('PATH', [System.StringComparison]::OrdinalIgnoreCase)) { continue }
    [Environment]::SetEnvironmentVariable($name, $line.Substring($separator + 1), 'Process')
}

$vsPathLine = $vsEnvironment | Where-Object { $_.StartsWith('PATH=', [System.StringComparison]::Ordinal) } | Select-Object -First 1
if (-not $vsPathLine) {
    throw 'Visual Studio developer environment did not provide PATH.'
}
$env:Path = $vsPathLine.Substring(5)
$env:Path = (Join-Path $env:USERPROFILE '.cargo\bin') + ';' + $env:Path
$linker = (Get-Command link.exe -CommandType Application -ErrorAction Stop).Source
if (-not $linker.StartsWith($vsInstall, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Expected the Visual Studio linker, but resolved $linker."
}
