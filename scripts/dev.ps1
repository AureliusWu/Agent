$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$frontend = Join-Path $root 'desktop\frontend'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$configuredEnvironment = $env:AGENT_ENV_FILE
if (-not $configuredEnvironment) { $configuredEnvironment = [Environment]::GetEnvironmentVariable('AGENT_ENV_FILE', 'User') }
$environment = if ($configuredEnvironment) { $configuredEnvironment } else { Join-Path $backend '.env' }
$environmentExample = Join-Path $backend '.env.example'
$devDataRoot = $env:AGENT_DATA_ROOT
if (-not $devDataRoot) { $devDataRoot = [Environment]::GetEnvironmentVariable('AGENT_DATA_ROOT', 'User') }
if (-not $devDataRoot) { $devDataRoot = Join-Path $env:LOCALAPPDATA 'AureliusWu\Agent-Dev' }

if (-not (Test-Path $environment)) {
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $environment) | Out-Null
  Copy-Item -LiteralPath $environmentExample -Destination $environment
  Write-Host 'Created local configuration from .env.example. Keep credentials outside the public checkout.' -ForegroundColor Yellow
}

if (-not (Test-Path $python)) {
  Write-Host 'Creating the backend virtual environment...' -ForegroundColor Cyan
  $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
  if (Test-Path -LiteralPath $bundledPython) {
    & $bundledPython -m venv (Join-Path $backend '.venv')
  } else {
    py -3.12 -m venv (Join-Path $backend '.venv')
  }
}
& $python (Join-Path $root 'scripts\check-python-runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Development and release environments require Python 3.12.' }
& $python -m pip install -r (Join-Path $backend 'requirements.lock')
& $python -m pip install --no-deps -e $backend

Write-Host 'Starting API at http://127.0.0.1:8000 ...' -ForegroundColor Cyan
$previousRuntimeEnvironment = $env:AGENT_RUNTIME_ENV
$previousDataRoot = $env:AGENT_DATA_ROOT
$previousEnvironmentFile = $env:AGENT_ENV_FILE
$env:AGENT_RUNTIME_ENV = 'development'
$env:AGENT_DATA_ROOT = $devDataRoot
$env:AGENT_ENV_FILE = $environment
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
  $env:AGENT_ENV_FILE = $previousEnvironmentFile
}
