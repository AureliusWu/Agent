param(
    [Parameter(Mandatory = $true)]
    [string]$Report,
    [string]$Baseline,
    [string]$Policy = 'backend/evals/gate-policy.json'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'backend\.venv\Scripts\python.exe'
$arguments = @('-m', 'app.evals.cli', 'gate', '--report', $Report, '--policy', $Policy)
if ($Baseline) {
    $arguments += @('--baseline', $Baseline)
}

Push-Location $root
try {
    & $python @arguments
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($exitCode -ne 0) {
    throw "Stable release gate rejected the report (exit code $exitCode)."
}
