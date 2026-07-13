param(
    [string]$Binary = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Binary) {
    $Binary = Join-Path $root 'frontend\src-tauri\binaries\agent-backend-x86_64-pc-windows-msvc.exe'
}
$resolvedBinary = (Resolve-Path -LiteralPath $Binary).Path
$smokeDirectory = Join-Path $root ('build\sidecar-smoke-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
New-Item -ItemType Directory -Path $smokeDirectory -Force | Out-Null

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$token = [Guid]::NewGuid().ToString('N')
$environment = @{
    AGENT_PORT = [string]$port
    AGENT_DATABASE_PATH = Join-Path $smokeDirectory 'agent.db'
    AGENT_LOG_PATH = Join-Path $smokeDirectory 'agent.log'
    AGENT_API_TOKEN = $token
}
$previousEnvironment = @{}
foreach ($entry in $environment.GetEnumerator()) {
    $previousEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process')
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
}

$process = $null
$ownedProcessIds = [Collections.Generic.HashSet[int]]::new()
try {
    $process = Start-Process -FilePath $resolvedBinary -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $smokeDirectory 'stdout.log') `
        -RedirectStandardError (Join-Path $smokeDirectory 'stderr.log')
    [void]$ownedProcessIds.Add($process.Id)
    $health = $null
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $health) { throw 'Packaged sidecar did not become ready.' }

    $headers = @{ 'X-Agent-Api-Token' = $token }
    $profiles = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/agent-profiles" -Headers $headers -TimeoutSec 5
    $policy = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/provider/policy" -Headers $headers -TimeoutSec 5
    $result = [pscustomobject]@{
        status = $health.status
        version = $health.version
        schema = $health.database.schema_version
        expected_schema = $health.database.expected_schema_version
        profile_ids = @($profiles | ForEach-Object { $_.id })
        multi_agent_enabled = $policy.multi_agent.enabled
    }
    if ($result.status -ne 'ok' -or $result.schema -ne $result.expected_schema) {
        throw 'Packaged sidecar health or database schema check failed.'
    }
    $result | ConvertTo-Json -Depth 5
} finally {
    for ($pass = 0; $pass -lt 5; $pass++) {
        $discovered = $false
        foreach ($candidate in Get-CimInstance Win32_Process) {
            if ($ownedProcessIds.Contains([int]$candidate.ParentProcessId)) {
                $discovered = $ownedProcessIds.Add([int]$candidate.ProcessId) -or $discovered
            }
        }
        if (-not $discovered) { break }
    }
    foreach ($processId in @($ownedProcessIds) | Sort-Object -Descending) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    foreach ($entry in $previousEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
}
