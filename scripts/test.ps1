$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'siyi'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$testDataRoot = Join-Path ([IO.Path]::GetTempPath()) ('siyi-release-tests-' + [Guid]::NewGuid().ToString('N'))
$previousDataRoot = [Environment]::GetEnvironmentVariable('AGENT_DATA_ROOT', 'Process')
$previousDesktopDataRoot = [Environment]::GetEnvironmentVariable('AGENT_DESKTOP_DATA_DIRECTORY', 'Process')
function Clear-CoverageData {
    Get-ChildItem -LiteralPath $backend -Filter '.coverage*' -File -ErrorAction SilentlyContinue | Remove-Item -Force
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Backend virtual environment is missing. Run scripts/dev.ps1 first.'
}
New-Item -ItemType Directory -Path $testDataRoot | Out-Null
$env:AGENT_DATA_ROOT = $testDataRoot
$env:AGENT_DESKTOP_DATA_DIRECTORY = $testDataRoot
try {
    Clear-CoverageData
    & $python (Join-Path $root 'scripts\check-release-metadata.py')
    if ($LASTEXITCODE -ne 0) {
        throw "Release metadata validation failed with exit code $LASTEXITCODE."
    }
    & $python (Join-Path $root 'scripts\generate_build_info.py') --build-type Development
    if ($LASTEXITCODE -ne 0) {
        throw "Development build fingerprint generation failed with exit code $LASTEXITCODE."
    }
    Push-Location $backend
    try {
        .\.venv\Scripts\python -m pytest -q -p no:cacheprovider --cov-fail-under=80
        $backendExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
        Clear-CoverageData
    }
    if ($backendExitCode -ne 0) {
        throw "Backend tests or the 80% coverage gate failed with exit code $backendExitCode."
    }
    Push-Location (Join-Path $root 'desktop\frontend')
    try {
        npm run lint
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend lint failed with exit code $LASTEXITCODE."
        }
        npm run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE."
        }
        npm run test:security
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend security check failed with exit code $LASTEXITCODE."
        }
        npm run test:desktop
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend desktop reliability tests failed with exit code $LASTEXITCODE."
        }
    } finally {
        Pop-Location
    }
    cargo test --locked --manifest-path (Join-Path $root 'desktop\src-tauri\Cargo.toml')
    if ($LASTEXITCODE -ne 0) {
        throw "Rust tests failed with exit code $LASTEXITCODE."
    }
} finally {
    Clear-CoverageData
    [Environment]::SetEnvironmentVariable('AGENT_DATA_ROOT', $previousDataRoot, 'Process')
    [Environment]::SetEnvironmentVariable('AGENT_DESKTOP_DATA_DIRECTORY', $previousDesktopDataRoot, 'Process')
    if (Test-Path -LiteralPath $testDataRoot -PathType Container) {
        $resolvedTestData = [IO.Path]::GetFullPath($testDataRoot)
        $tempBoundary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        ) + [IO.Path]::DirectorySeparatorChar
        if (-not ($resolvedTestData + [IO.Path]::DirectorySeparatorChar).StartsWith(
            $tempBoundary,
            [StringComparison]::OrdinalIgnoreCase
        ) -or (Split-Path -Leaf $resolvedTestData) -notlike 'siyi-release-tests-*') {
            throw "Refusing to clean a non-test data directory: $resolvedTestData"
        }
        Remove-Item -LiteralPath $resolvedTestData -Recurse -Force
    }
}
