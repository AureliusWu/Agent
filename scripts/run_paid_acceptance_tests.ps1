$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($env:SIYI_TEST_PROVIDER -ne 'deepseek' -or $env:SIYI_ALLOW_PAID_API -ne 'true') {
    throw 'BLOCKED: paid acceptance requires SIYI_TEST_PROVIDER=deepseek and SIYI_ALLOW_PAID_API=true.'
}

$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$env:PYTHONPATH = Join-Path $root 'siyi'
Push-Location $root
try {
    & $python -m pytest tests/backend/providers/test_deepseek_paid_live.py -q -m paid_model
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
}
exit $exitCode

