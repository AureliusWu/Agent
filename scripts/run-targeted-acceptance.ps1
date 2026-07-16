param(
    [string]$Label = "local",
    [switch]$IncludeDesktop
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Output = Join-Path $Root "build\acceptance\$Label-targeted"
$LogDirectory = Join-Path $Output "logs"
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

$BuildInfo = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "build\generated\build-info.json") | ConvertFrom-Json
$BuildId = [string]$BuildInfo.build_id
$RecordedAt = (Get-Date).ToUniversalTime().ToString("o")
$Gates = @{}

function Invoke-EvidenceCommand {
    param([string]$Gate, [string]$Command, [scriptblock]$Action, [string]$Type = "automated")
    $Log = Join-Path $LogDirectory "$Gate.log"
    try {
        $PreviousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $Action *>&1 | Tee-Object -FilePath $Log
        $ExitCode = $LASTEXITCODE
        $ErrorActionPreference = $PreviousErrorAction
        if ($ExitCode -and $ExitCode -ne 0) { throw "$Gate exited with $ExitCode" }
        $Status = "passed"
    } catch {
        $ErrorActionPreference = $PreviousErrorAction
        $_ | Out-String | Add-Content -Encoding UTF8 $Log
        $Status = "failed"
    }
    $script:Gates[$Gate] = @{
        status = $Status
        evidence_type = $Type
        command = $Command
        artifact = $Log
        recorded_at = $RecordedAt
        build_id = $BuildId
    }
}

Push-Location $Root
try {
    Invoke-EvidenceCommand "backend" ".\scripts\test.ps1" { .\scripts\test.ps1 }
    $Gates["frontend"] = $Gates["backend"].Clone()
    $Gates["frontend"]["command"] = ".\scripts\test.ps1 (frontend lint/build/security stages)"

    Invoke-EvidenceCommand "rust" "cargo test --manifest-path frontend/src-tauri/Cargo.toml --locked" {
        cmd /d /c "cargo test --manifest-path frontend\src-tauri\Cargo.toml --locked"
    }
    Invoke-EvidenceCommand "v4" "core + adversarial + multi-agent + professional eval suites" {
        .\scripts\eval.ps1 -Mode scripted_runtime -Label "$Label-core" -Suite core
        .\scripts\eval.ps1 -Mode adversarial -Label "$Label-security" -Suite adversarial -Tasks backend/evals/adversarial_tasks.json
        .\scripts\eval.ps1 -Mode scripted_runtime -Label "$Label-multi" -Suite multi_agent -Tasks backend/evals/multi_agent_tasks.json
        .\scripts\eval.ps1 -Mode scripted_runtime -Label "$Label-professional" -Suite professional_agents -Tasks backend/evals/professional_agent_tasks.json
    }

    if ($IncludeDesktop) {
        Invoke-EvidenceCommand "desktop" ".\scripts\build-desktop.ps1" { .\scripts\build-desktop.ps1 } "e2e"
        $Gates["release"] = $Gates["desktop"].Clone()
    }

    $GatesPath = Join-Path $Output "gates.json"
    $Gates | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $GatesPath
    & .\backend\.venv\Scripts\python.exe .\scripts\assess-targeted-acceptance.py `
        --gates $GatesPath --output (Join-Path $Output "report.json")
    if ($LASTEXITCODE -ne 0) { throw "Targeted acceptance assessment failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
