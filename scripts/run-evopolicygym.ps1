param(
    [string]$EvoRoot = '',
    [int]$Repeats = 3,
    [int]$Budget = 8,
    [string]$EvidenceRoot = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -Raw -Encoding UTF8 (Join-Path $root 'evals\evopolicygym.lock.json') | ConvertFrom-Json
$expectedCommit = [string]$lock.commit
if (-not $EvoRoot) {
    $EvoRoot = Join-Path $root 'build\third_party\EvoPolicyGym'
}
$EvoRoot = [IO.Path]::GetFullPath($EvoRoot)
if (-not (Test-Path -LiteralPath (Join-Path $EvoRoot '.git'))) {
    throw "Pinned EvoPolicyGym checkout is missing: $EvoRoot"
}
$actualCommit = (& git -C $EvoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $expectedCommit) {
    throw "EvoPolicyGym commit mismatch: expected $expectedCommit, found $actualCommit"
}
if ($Repeats -lt 1 -or $Budget -lt 1) {
    throw 'Repeats and Budget must be positive.'
}
if (-not $EvidenceRoot) {
    $EvidenceRoot = Join-Path $root 'build\v8-evidence\evopolicygym'
}
$EvidenceRoot = [IO.Path]::GetFullPath($EvidenceRoot)
New-Item -ItemType Directory -Force -Path $EvidenceRoot | Out-Null
$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$sessionRoot = Join-Path ([IO.Path]::GetTempPath()) "Siyi-Evals\evopolicygym-$runId"
New-Item -ItemType Directory -Force -Path $sessionRoot | Out-Null
$adapter = Join-Path $root 'scripts\siyi-evo-adapter.py'
$python = Join-Path $root 'siyi\.venv\Scripts\python.exe'
$buildInfo = Get-Content -Raw -Encoding UTF8 (Join-Path $root 'build\generated\build-info.json') | ConvertFrom-Json
$summary = [ordered]@{
    schema_version = 1
    status = 'running'
    evopolicygym_repository = [string]$lock.repository
    evopolicygym_commit = $actualCommit
    siyi_commit = (& git -C $root rev-parse HEAD).Trim()
    build_id = [string]$buildInfo.build_id
    started_at = (Get-Date).ToUniversalTime().ToString('o')
    session_root = "<isolated-temp>/Siyi-Evals/evopolicygym-$runId"
    isolated_data_root = '<isolated-temp>/Siyi-Evals'
    model_key_configured = [bool]$env:SIYI_EVO_MODEL_API_KEY
    runs = @()
}
$summaryPath = Join-Path $EvidenceRoot "$runId-summary.json"
if (-not $env:SIYI_EVO_MODEL_API_KEY) {
    $summary.status = 'blocked'
    $summary.block_reason = 'SIYI_EVO_MODEL_API_KEY is not configured; real Siyi model execution cannot start.'
    $summary.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $summary | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 $summaryPath
    throw "EvoPolicyGym blocked: SIYI_EVO_MODEL_API_KEY is not configured. Evidence: $summaryPath"
}

Push-Location $EvoRoot
try {
    foreach ($environment in @('toy', 'cartpole')) {
        $checkLog = Join-Path $EvidenceRoot "$runId-$environment-check-envs.log"
        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & uv run evopolicygym check-envs --env $environment *>&1 | Tee-Object -FilePath $checkLog
        $checkExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($checkExitCode -ne 0) {
            throw "EvoPolicyGym environment check failed: $environment"
        }
        for ($repeat = 1; $repeat -le $Repeats; $repeat++) {
            $name = "$environment-repeat-$repeat"
            $runRoot = Join-Path $sessionRoot $name
            $log = Join-Path $EvidenceRoot "$runId-$name.log"
            $previousErrorAction = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            & uv run evopolicygym run `
                --env $environment `
                --root $runRoot `
                --model siyi-runtime `
                --exp-id $name `
                --budget $Budget `
                --minimum 1 `
                --maximum $Budget `
                --valid-size 4 `
                --final-size 8 `
                --limit 32 `
                --retries 1 `
                --retry-backoff 1 `
                --bind 127.0.0.1 `
                --port 0 `
                --agent command `
                --agent-name siyi `
                -- $python $adapter *>&1 | Tee-Object -FilePath $log
            $exitCode = $LASTEXITCODE
            $ErrorActionPreference = $previousErrorAction
            $runJson = Join-Path $runRoot 'run.json'
            if (-not (Test-Path -LiteralPath $runJson)) {
                throw "EvoPolicyGym did not produce run.json for $name"
            }
            $evidenceRun = Join-Path $EvidenceRoot "$runId-$name-run.json"
            Copy-Item -LiteralPath $runJson -Destination $evidenceRun -Force
            $payload = Get-Content -Raw -Encoding UTF8 $runJson | ConvertFrom-Json
            $summary.runs += [ordered]@{
                environment = $environment
                repeat = $repeat
                exit_code = $exitCode
                status = [string]$payload.outcome.status
                final_score = $payload.outcome.final_score
                heldout_mean_return = $payload.outcome.heldout_mean_return
                wall_time_seconds = $payload.timing.wall_time_seconds
                run_json = "build/v8-evidence/evopolicygym/$([IO.Path]::GetFileName($evidenceRun))"
                log = "build/v8-evidence/evopolicygym/$([IO.Path]::GetFileName($log))"
            }
            if ($exitCode -ne 0 -or [string]$payload.outcome.status -ne 'completed') {
                throw "EvoPolicyGym run failed: $name"
            }
        }
    }
    $summary.status = 'passed'
} catch {
    $summary.status = 'failed'
    $summary.failure = $_.Exception.Message
    throw
} finally {
    $summary.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $summary | ConvertTo-Json -Depth 10 | Set-Content -Encoding UTF8 $summaryPath
    Pop-Location
}
Write-Output $summaryPath
