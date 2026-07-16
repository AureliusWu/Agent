$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$targets = @(
  (Join-Path $root 'build'),
  (Join-Path $root 'dist'),
  (Join-Path $root 'backend\build'),
  (Join-Path $root 'backend\dist'),
  (Join-Path $root 'backend\.pytest_cache'),
  (Join-Path $root '.pytest_cache'),
  (Join-Path $root 'frontend\dist'),
  (Join-Path $root 'frontend\src-tauri\target'),
  (Join-Path $root '.coverage'),
  (Join-Path $root 'backend\agent-backend.spec')
)

foreach ($target in $targets) {
  $full = [System.IO.Path]::GetFullPath($target)
  if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean outside repository: $full"
  }
  if (Test-Path -LiteralPath $full) {
    Remove-Item -LiteralPath $full -Recurse -Force
    Write-Host "Removed $full"
  }
}
