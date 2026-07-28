$ErrorActionPreference = 'Stop'

if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Host 'Ollama is already installed.'
    & ollama --version
    exit $LASTEXITCODE
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Windows Package Manager is unavailable. Install Ollama from https://ollama.com/download/windows.'
}
Write-Host 'Installing publisher-verified Ollama.Ollama package through Windows Package Manager.'
& winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
if ($LASTEXITCODE -ne 0) {
    throw "Ollama installation failed with exit code $LASTEXITCODE."
}

$ollamaDirectory = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
if (Test-Path -LiteralPath $ollamaDirectory) {
    $env:Path = "$ollamaDirectory;$env:Path"
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Ollama installation completed but ollama.exe is not available.'
}
& ollama --version
