param(
    [ValidateSet('scripted_runtime', 'live_model')]
    [string]$Mode = 'scripted_runtime',
    [string]$Label = 'local',
    [string]$Suite = 'core',
    [string]$Output = 'data/evals',
    [string[]]$TaskId = @()
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.'
}

Push-Location $root
try {
    $arguments = @('-m', 'app.evals.cli', 'run', '--label', $Label, '--mode', $Mode, '--suite', $Suite, '--output', $Output)
    foreach ($id in $TaskId) {
        $arguments += @('--task', $id)
    }
    & $python @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($exitCode -ne 0) {
    throw "Agent Eval failed with exit code $exitCode."
}
