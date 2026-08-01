param(
    [string]$Binary = '',
    [string]$Output = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Binary) { $Binary = Join-Path $root 'desktop\src-tauri\binaries\agent-backend-x86_64-pc-windows-msvc.exe' }
if (-not $Output) { $Output = Join-Path $root 'build\v130-evidence\packaged-local-runtime-smoke.json' }
$smoke = Join-Path $root ('build\packaged-local-runtime-' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
New-Item -ItemType Directory -Path $smoke -Force | Out-Null
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start(); $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port; $listener.Stop()
$token = [Guid]::NewGuid().ToString('N')
$environment = @{
    AGENT_PORT = [string]$port; AGENT_DEPLOYMENT_MODE = 'desktop_local'; AGENT_BIND_HOST = '127.0.0.1'
    AGENT_DATA_ROOT = Join-Path $smoke 'runtime'; AGENT_DATABASE_PATH = Join-Path $smoke 'agent.db'
    AGENT_LOG_PATH = Join-Path $smoke 'agent.log'; AGENT_API_TOKEN = $token
}
$previous = @{}
foreach ($entry in $environment.GetEnumerator()) { $previous[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process'); [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
$process = $null
try {
    $resolvedBinary = (Resolve-Path -LiteralPath $Binary).Path
    $process = Start-Process -FilePath $resolvedBinary -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $smoke 'stdout.log') -RedirectStandardError (Join-Path $smoke 'stderr.log')
    $health = $null
    for ($attempt = 0; $attempt -lt 80; $attempt++) { try { $health = Invoke-RestMethod "http://127.0.0.1:$port/api/health" -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 500 } }
    if ($null -eq $health) { throw 'Packaged sidecar did not become ready.' }
    $headers = @{ 'X-Agent-Api-Token' = $token }
    $ttsHealth = Invoke-RestMethod "http://127.0.0.1:$port/api/tts/health" -Headers $headers -TimeoutSec 15
    $voices = Invoke-RestMethod "http://127.0.0.1:$port/api/tts/voices?provider=windows" -Headers $headers -TimeoutSec 15
    $chineseVoices = @($voices | Where-Object { $_.culture -eq 'zh-CN' })
    if ($chineseVoices.Count -eq 0) { throw 'Packaged Windows TTS has no Chinese voice.' }
    $body = @{
        request_id = 'packaged-tts'
        idempotency_key = 'packaged-tts'
        text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5L2g5aW977yM6L+Z5piv5q2j5byPIFNpZGVjYXIg55qE5Lit5paH6K+t6Z+z6aqM6K+B44CC'))
        cache = $false
    } | ConvertTo-Json
    $speech = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$port/api/tts/synthesize" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 30
    $wave = Join-Path $smoke 'speech.wav'
    Invoke-WebRequest -Uri "http://127.0.0.1:$port$($speech.audio_url)" -Headers $headers -OutFile $wave -TimeoutSec 15
    $bytes = [IO.File]::ReadAllBytes($wave)
    if ($bytes.Length -le 10000 -or [Text.Encoding]::ASCII.GetString($bytes,0,4) -ne 'RIFF') { throw 'Packaged TTS WAV validation failed.' }
    $localModels = Invoke-RestMethod "http://127.0.0.1:$port/api/local-models/service" -Headers $headers -TimeoutSec 10
    $result = [ordered]@{
        status = 'PASS'
        recorded_at = [DateTimeOffset]::UtcNow.ToString('o')
        binary = $resolvedBinary
        version = $health.version
        build_id = $health.build.build_id
        component_build_id = $health.build.component_build_id
        schema = $health.database.schema_version
        tts_health = $ttsHealth.status
        windows_voice_count = @($voices).Count
        wav_bytes = $bytes.Length
        duration_ms = $speech.duration_ms
        provider = $speech.provider
        ollama_status = $localModels.status
        ollama_mode = $localModels.mode
        ollama_pid = $localModels.listener_pid
    }
    $outputDirectory = Split-Path -Parent $Output
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding UTF8
    $result | ConvertTo-Json -Depth 8
} finally {
    if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force; $process.WaitForExit(10000) | Out-Null }
    if ($resolvedBinary) {
        Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $resolvedBinary } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($entry in $previous.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
}
