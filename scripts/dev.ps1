$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$frontend = Join-Path $root 'desktop\frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$environment = Join-Path $backend '.env'
$environmentExample = Join-Path $backend '.env.example'
$devDataRoot = Join-Path $env:LOCALAPPDATA 'AureliusWu\Agent-Dev'

if (-not (Test-Path $environment)) {
  Copy-Item -LiteralPath $environmentExample -Destination $environment
  Write-Host 'Created siyi/.env from .env.example. Add an API key there for browser mode.' -ForegroundColor Yellow
}

if (-not (Test-Path $python)) {
  Write-Host 'Creating the backend virtual environment...' -ForegroundColor Cyan
  py -m venv (Join-Path $backend '.venv')
}
& $python -m pip install -r (Join-Path $backend 'requirements.lock')
& $python -m pip install --no-deps -e $backend

Write-Host 'Starting API at http://127.0.0.1:8000 ...' -ForegroundColor Cyan
$previousRuntimeEnvironment = $env:AGENT_RUNTIME_ENV
$previousDataRoot = $env:AGENT_DATA_ROOT
$env:AGENT_RUNTIME_ENV = 'development'
$env:AGENT_DATA_ROOT = $devDataRoot
$backendProc = Start-Process -FilePath $python `
  -ArgumentList '-m','uvicorn','app.main:app','--reload','--host','127.0.0.1','--port','8000' `
  -WorkingDirectory $backend -WindowStyle Hidden -PassThru

Write-Host 'Starting PWA at http://localhost:5173 ...' -ForegroundColor Cyan
$frontendProc = Start-Process -FilePath 'npm.cmd' `
  -ArgumentList 'run','dev' -WorkingDirectory $frontend -WindowStyle Hidden -PassThru

function Stop-DevelopmentServices {
  Write-Host 'Stopping development services...' -ForegroundColor Yellow
  foreach ($process in @($backendProc, $frontendProc)) {
    if ($process -and -not $process.HasExited) {
      & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    }
  }
}

Write-Host 'Development environment is ready. Press Ctrl+C to stop.' -ForegroundColor Green
try {
  while ($true) {
    Start-Sleep -Seconds 1
    if ($backendProc.HasExited -or $frontendProc.HasExited) {
      throw 'A development service exited unexpectedly.'
    }
  }
}
finally {
  Stop-DevelopmentServices
  $env:AGENT_RUNTIME_ENV = $previousRuntimeEnvironment
  $env:AGENT_DATA_ROOT = $previousDataRoot
}
