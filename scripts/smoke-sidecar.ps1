param(
    [string]$Binary = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Binary) {
    $Binary = Join-Path $root 'desktop\src-tauri\binaries\agent-backend-x86_64-pc-windows-msvc.exe'
}
$resolvedBinary = (Resolve-Path -LiteralPath $Binary).Path
$smokeDirectory = Join-Path $root ('build\sidecar-smoke-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
New-Item -ItemType Directory -Path $smokeDirectory -Force | Out-Null

$normalizedPath = $env:PATH
[Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
[Environment]::SetEnvironmentVariable('Path', $normalizedPath, 'Process')

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$token = [Guid]::NewGuid().ToString('N')
$environment = @{
    AGENT_PORT = [string]$port
    AGENT_DEPLOYMENT_MODE = 'desktop_local'
    AGENT_BIND_HOST = '127.0.0.1'
    AGENT_DATA_ROOT = Join-Path $smokeDirectory 'runtime'
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
    $readinessTimer = [Diagnostics.Stopwatch]::StartNew()
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
    $readinessTimer.Stop()

    $headers = @{ 'X-Agent-Api-Token' = $token }
    $profiles = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/agent-profiles" -Headers $headers -TimeoutSec 5
    $policy = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/provider/policy" -Headers $headers -TimeoutSec 5
    $diagnostics = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/diagnostics/status" -Headers $headers -TimeoutSec 5
    $result = [pscustomobject]@{
        status = $health.status
        version = $health.version
        build_id = $health.build.build_id
        component_build_id = $health.build.component_build_id
        source_fingerprint = $health.build.source_fingerprint
        workspace_state = $health.build.workspace_state
        build_type = $health.build.build_type
        build_embedded = $health.build.embedded
        schema = $health.database.schema_version
        expected_schema = $health.database.expected_schema_version
        deployment_mode = $health.deployment.mode
        bind_host = $health.deployment.bind_host
        loopback = $health.deployment.loopback
        kernel_contract = $health.kernel.contract_version
        kernel_replaceable = $health.kernel.extension_replaceable
        readiness_ms = $readinessTimer.ElapsedMilliseconds
        profile_ids = @($profiles | ForEach-Object { $_.id })
        multi_agent_enabled = $policy.multi_agent.enabled
        hooks_available = $null -ne $diagnostics.capabilities.hooks
        lsp_fallback = $diagnostics.capabilities.lsp.fallback
        mcp_ttl_seconds = $diagnostics.capabilities.mcp.ttl_seconds
        managed_worktrees = $diagnostics.capabilities.managed_worktrees
    }
    if ($result.status -ne 'ok' -or $result.schema -ne $result.expected_schema) {
        throw 'Packaged sidecar health or database schema check failed.'
    }
    if (-not $result.build_embedded -or -not $result.build_id -or $result.build_id -eq 'unavailable') {
        throw 'Packaged sidecar does not contain an embedded build manifest.'
    }
    if ($result.deployment_mode -ne 'desktop_local' -or $result.bind_host -ne '127.0.0.1' -or -not $result.loopback) {
        throw 'Packaged sidecar deployment boundary check failed.'
    }
    if ($result.kernel_contract -ne '1.2' -or $result.kernel_replaceable) {
        throw 'Packaged sidecar kernel contract check failed.'
    }
    if (-not $result.hooks_available -or $result.lsp_fallback -ne 'workspace_index' -or $result.mcp_ttl_seconds -le 0 -or -not $result.managed_worktrees) {
        throw 'Packaged sidecar v4 capability diagnostics check failed.'
    }
    $result | ConvertTo-Json -Depth 5
} finally {
    for ($pass = 0; $pass -lt 5; $pass++) {
        $discovered = $false
        try {
            $candidates = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        } catch {
            $candidates = @()
        }
        foreach ($candidate in $candidates) {
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
