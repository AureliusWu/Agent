$ErrorActionPreference = 'Stop'
$requiredModel = 'qwen3:4b'
$command = Get-Command ollama -ErrorAction SilentlyContinue
$result = [ordered]@{
    installed = [bool]$command
    executable = if ($command) { $command.Source } else { $null }
    service = $false
    required_model = $requiredModel
    model_installed = $false
    version = $null
}

if ($command) {
    $result.version = (& ollama --version 2>&1 | Out-String).Trim()
    try {
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
        $result.service = $true
        $result.model_installed = @($tags.models | ForEach-Object { $_.name }) -contains $requiredModel
    } catch {
        $result.service_error = $_.Exception.Message
    }
}

$result | ConvertTo-Json -Depth 4
if (-not $result.installed -or -not $result.service -or -not $result.model_installed) {
    exit 2
}

