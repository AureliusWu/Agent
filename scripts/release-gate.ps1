param(
    [Parameter(Mandatory = $true)]
    [string]$Report,
    [string]$Baseline,
    [string]$Policy = 'evals/gate-policy.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$arguments = @('-m', 'app.evals.cli', 'gate', '--report', $Report, '--policy', $Policy)
if ($Baseline) {
    $arguments += @('--baseline', $Baseline)
}

$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$env:PYTHONPATH = if ($previousPythonPath) {
    $backend + [IO.Path]::PathSeparator + $previousPythonPath
} else {
    $backend
}
Push-Location $root
try {
    & $python @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
}
if ($exitCode -ne 0) {
    throw "Stable release gate rejected the report (exit code $exitCode)."
}
