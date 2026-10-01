param(
    [Parameter(Mandatory = $true)]
    [string]$Report,
    [Parameter(Mandatory = $true)]
    [string]$Baseline,
    [Parameter(Mandatory = $true)]
    [ValidateSet('scripted_runtime', 'live_model', 'adversarial')]
    [string]$Mode,
    [string]$Suite = 'core',
    [string]$Tasks = 'evals/tasks.json',
    [string]$Policy = 'evals/gate-policy.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$arguments = @('-m', 'app.evals.cli', 'gate', '--report', $Report, '--policy', $Policy, '--mode', $Mode, '--suite', $Suite, '--tasks', $Tasks)
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
    throw "Evaluation gate rejected the report (exit code $exitCode). This is not the desktop release acceptance gate."
}
