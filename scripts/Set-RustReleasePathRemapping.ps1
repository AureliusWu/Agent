function Get-RustReleaseHostPathMappings([string]$RepositoryRoot) {
    # rustc uses the last matching mapping. Put more specific roots after the
    # profile so that diagnostic locations remain useful without host paths.
    $roots = @(
        @{ Path = $env:USERPROFILE; Target = '/build/user' },
        @{ Path = $env:CARGO_HOME; Target = '/build/cargo' },
        @{ Path = $env:RUSTUP_HOME; Target = '/build/rustup' },
        @{ Path = $RepositoryRoot; Target = '/build/source' }
    )
    foreach ($entry in $roots) {
        if (-not $entry.Path) { continue }
        $source = [System.IO.Path]::GetFullPath($entry.Path).TrimEnd('\', '/')
        if ($source.Length -le 3 -or $source.Contains('=') -or $source.Contains([char]0x1f)) {
            throw 'Cannot safely remap a Rust release build path.'
        }
        foreach ($spelling in @($source, $source.Replace('\', '/')) | Select-Object -Unique) {
            @{ Source = $spelling; Target = $entry.Target }
        }
    }
}

function Set-RustReleasePathRemapping([string]$RepositoryRoot) {
    # Cargo gives the encoded form precedence. Preserve existing process flags
    # (including arguments containing spaces) rather than rebuilding RUSTFLAGS.
    $separator = [char]0x1f
    $flags = [System.Collections.Generic.List[string]]::new()
    $encoded = [Environment]::GetEnvironmentVariable('CARGO_ENCODED_RUSTFLAGS', 'Process')
    if ($null -ne $encoded) {
        foreach ($flag in $encoded.Split($separator)) { if ($flag) { $flags.Add($flag) } }
    } elseif ($env:RUSTFLAGS) {
        foreach ($flag in ($env:RUSTFLAGS -split '\s+')) { if ($flag) { $flags.Add($flag) } }
    }
    foreach ($entry in (Get-RustReleaseHostPathMappings $RepositoryRoot)) {
        $flags.Add("--remap-path-prefix=$($entry.Source)=$($entry.Target)")
    }
    $env:CARGO_ENCODED_RUSTFLAGS = $flags -join $separator
}

function Assert-NoRustReleaseHostPaths([string]$Binary, [string]$RepositoryRoot) {
    # Use a one-to-one byte representation; decoding a PE directly as text could
    # silently replace invalid byte sequences before an embedded path is found.
    $byteEncoding = [Text.Encoding]::GetEncoding(28591)
    $payload = $byteEncoding.GetString([IO.File]::ReadAllBytes($Binary))
    foreach ($entry in (Get-RustReleaseHostPathMappings $RepositoryRoot)) {
        foreach ($encoding in @([Text.Encoding]::UTF8, [Text.Encoding]::Unicode)) {
            $needle = $byteEncoding.GetString($encoding.GetBytes($entry.Source))
            if ($payload.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                throw 'Desktop executable contains an unmapped build-machine path; do not publish its bundles.'
            }
        }
    }
}
