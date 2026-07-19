param(
    [ValidateSet('scripted_runtime', 'live_model', 'adversarial')]
    [string]$Mode = 'scripted_runtime',
    [string]$Label = 'local',
    [string]$Suite = 'core',
    [string]$Output = 'data/evals',
    [string]$Tasks = '',
    [string[]]$TaskId = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.'
}

$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$env:PYTHONPATH = if ($previousPythonPath) {
    $backend + [IO.Path]::PathSeparator + $previousPythonPath
} else {
    $backend
}
Push-Location $root
try {
    $arguments = @('-m', 'app.evals.cli', 'run', '--label', $Label, '--mode', $Mode, '--suite', $Suite, '--output', $Output)
    if ($Tasks) {
        $arguments += @('--tasks', $Tasks)
    }
    foreach ($id in $TaskId) {
        $arguments += @('--task', $id)
    }
    & $python @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
}
if ($exitCode -ne 0) {
    throw "Agent Eval failed with exit code $exitCode."
}
