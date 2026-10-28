param()

# Generate local integrity pins after building/copying a desktop runtime.
# This activates bytes only; it does not run the app or qualify a release.
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskManifestPath = Join-Path $taskRoot '.siyi-desktop.local.json'
$taskDesktopName = ([string][char]0x53f8) + [char]0x5fc6 + '.exe'
$taskBuildPath = Join-Path $taskRoot '_internal\build-info.json'

function Assert-Unlinked([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked runtime paths are not accepted.' }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

Assert-Unlinked $taskRoot
Assert-Unlinked $taskBuildPath
Assert-Unlinked $taskManifestPath
$taskVersion = (Get-Content -LiteralPath (Join-Path $taskRoot 'VERSION') -Raw -Encoding UTF8).Trim()
$taskBuild = Get-Content -LiteralPath $taskBuildPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskBuild.product_version -cne $taskVersion -or $taskVersion -ne '16.0.0') {
    throw 'Root runtime must match VERSION 16.0.0. Build/copy the correct desktop runtime first.'
}
if ($taskBuild.git_commit -notmatch '^[0-9a-f]{40}$' -or -not $taskBuild.build_id) { throw 'Runtime provenance is incomplete.' }
$taskOwner = 'siyi-user-testing-v' + $taskVersion
if (Test-Path -LiteralPath $taskManifestPath) {
    $previous = Get-Content -LiteralPath $taskManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($previous.product_version -cne $taskVersion -or $previous.data_owner -cne $taskOwner) {
        throw 'Existing activation belongs to another data profile; refusing to replace it.'
    }
}
$taskFiles = @(
    Get-Item -LiteralPath (Join-Path $taskRoot $taskDesktopName), (Join-Path $taskRoot 'agent-backend.exe') -Force
    Get-ChildItem -LiteralPath (Join-Path $taskRoot '_internal') -File -Recurse -Force
)
if ($taskFiles.Count -lt 3) { throw 'Incomplete desktop runtime.' }
$taskEntries = @($taskFiles | Sort-Object FullName | ForEach-Object {
    Assert-Unlinked $_.FullName
    $relative = $_.FullName.Substring($taskRoot.Length + 1).Replace('\', '/')
    [ordered]@{ path = $relative; bytes = $_.Length; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
})
$taskActivation = [ordered]@{
    schema_version = 1
    product_version = $taskVersion
    runtime_commit = $taskBuild.git_commit
    build_id = $taskBuild.build_id
    data_owner = $taskOwner
    activated_at_utc = [DateTime]::UtcNow.ToString('o')
    scope = 'local-user-test-baseline-not-formal-release'
    files = $taskEntries
}
if (Test-Path -LiteralPath $taskManifestPath) {
    $archive = Join-Path $taskRoot ('.siyi-desktop.local.previous-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.json')
    Copy-Item -LiteralPath $taskManifestPath -Destination $archive
}
$taskTemporary = $taskManifestPath + '.tmp-' + [Guid]::NewGuid().ToString('N')
try {
    [IO.File]::WriteAllText($taskTemporary, ($taskActivation | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $taskManifestPath) {
        # PowerShell can bind $null to an empty string for this .NET overload.
        # NullString preserves an actual null backup path on both PS 5.1 and 7.
        [IO.File]::Replace($taskTemporary, $taskManifestPath, [NullString]::Value)
    } else {
        [IO.File]::Move($taskTemporary, $taskManifestPath)
    }
} finally {
    if (Test-Path -LiteralPath $taskTemporary) { Remove-Item -LiteralPath $taskTemporary }
}
Write-Host ('Activated local v' + $taskVersion + ' runtime ' + $taskBuild.git_commit.Substring(0,7) + ' (' + $taskEntries.Count + ' files).')
Write-Host 'No application, model, installer or paid validation was executed.'
