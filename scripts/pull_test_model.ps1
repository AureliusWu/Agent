$ErrorActionPreference = 'Stop'
$requiredModel = 'qwen3:4b'

if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Ollama is not installed. Run scripts/install_ollama.ps1 first.'
}

Write-Host "Pulling the only approved local test model: $requiredModel"
& ollama pull $requiredModel
if ($LASTEXITCODE -ne 0) {
    throw "Failed to pull $requiredModel."
}

$models = & ollama list
if ($LASTEXITCODE -ne 0 -or ($models -join "`n") -notmatch [regex]::Escape($requiredModel)) {
    throw "$requiredModel was not found after pull."
}
Write-Host "$requiredModel is installed."

