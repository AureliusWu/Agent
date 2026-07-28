$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$previousPythonPath = [Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
$previousProvider = [Environment]::GetEnvironmentVariable('SIYI_TEST_PROVIDER', 'Process')
$previousPaid = [Environment]::GetEnvironmentVariable('SIYI_ALLOW_PAID_API', 'Process')
$env:PYTHONPATH = Join-Path $root 'siyi'
$env:SIYI_TEST_PROVIDER = 'ollama'
[Environment]::SetEnvironmentVariable('SIYI_ALLOW_PAID_API', $null, 'Process')

Push-Location $root
try {
    & (Join-Path $root 'scripts\check_ollama.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'Ollama or qwen3:4b is unavailable.'
    }
    & $python -m pytest tests/backend/providers/test_ollama_live.py -q -m local_model
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
    [Environment]::SetEnvironmentVariable('SIYI_TEST_PROVIDER', $previousProvider, 'Process')
    [Environment]::SetEnvironmentVariable('SIYI_ALLOW_PAID_API', $previousPaid, 'Process')
}
exit $exitCode

