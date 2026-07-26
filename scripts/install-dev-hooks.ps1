$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Push-Location $root
try {
    git config --local core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to configure the repository hooks path."
    }
    Write-Output "Configured core.hooksPath=.githooks"
} finally {
    Pop-Location
}
