[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryRoot,
    # Release tests may be collected before the final version source is
    # advanced.  This explicit target keeps new evidence out of the old
    # release directory without misrepresenting VERSION itself.
    [string]$EvidenceVersion = ''
)

$resolvedRoot = (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).Path
$versionPath = Join-Path $resolvedRoot 'VERSION'
if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) {
    throw "Release version source is missing: $versionPath"
}

$version = if ($EvidenceVersion) {
    $EvidenceVersion.Trim()
} else {
    (Get-Content -LiteralPath $versionPath -Raw -Encoding ascii).Trim()
}
if ($EvidenceVersion -and $version -match '^v(?=[0-9])') {
    $version = $version.Substring(1)
}
if ([string]::IsNullOrWhiteSpace($version) -or $version -notmatch '^[0-9A-Za-z.+-]+$') {
    throw "Evidence version is not safe for an evidence directory: '$version'"
}

$versionCompact = $version -replace '[^0-9A-Za-z]', ''
if ([string]::IsNullOrWhiteSpace($versionCompact)) {
    throw "Release version has no compact form: '$version'"
}

Join-Path $resolvedRoot ("build\v{0}-evidence" -f $versionCompact)
