$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'

if (-not (Test-Path (Join-Path $backend '.venv\Scripts\python.exe'))) {
  py -m venv (Join-Path $backend '.venv')
  & (Join-Path $backend '.venv\Scripts\python.exe') -m pip install -r (Join-Path $backend 'requirements.txt')
}

Start-Process -FilePath (Join-Path $backend '.venv\Scripts\python.exe') `
  -ArgumentList '-m','uvicorn','app.main:app','--reload','--host','127.0.0.1','--port','8000' `
  -WorkingDirectory $backend -WindowStyle Hidden

Set-Location $frontend
npm run dev
