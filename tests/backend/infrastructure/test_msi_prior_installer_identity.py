from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PyInstaller.archive.writers import CArchiveWriter


ROOT = Path(__file__).resolve().parents[3]
PRIOR_MSI_SHA256 = "BE220314642C9E4F73735C671ED4AA2AE8871BF081311BADE96380A370FD349E"
PRIOR_SIDECAR_SHA256 = "47BAC2E0EDE6AEF2A4AB6864E4C56D5C35B9E9B7BCCFDEAB6E8421EB70F35494"
COMMIT = "1" * 40
FINGERPRINT = "2" * 64
BUILD_ID = "3" * 24
PROBE = r"""
$ErrorActionPreference = 'Stop'
# Use the child shell's own module even when it inherited PowerShell 7 paths.
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility')
$root = $env:SIYI_PROBE_ROOT
$python = $env:SIYI_PROBE_PYTHON
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $root 'scripts/smoke-msi.ps1'), [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw ($errors -join '; ') }
$definition = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Assert-InstalledBuildIdentity'
}, $false)
if (-not $definition) { throw 'Missing MSI identity check' }
# Load only the production function; never invoke the installer smoke script.
. ([scriptblock]::Create($definition.Extent.Text))
# Substitute the official file digest for only this synthetic archive fixture.
# An unmocked negative case still checks that its real digest is rejected.
function Get-FileHash {
    param([string]$LiteralPath, [string]$Algorithm = 'SHA256')
    if ($env:SIYI_PROBE_SIDECAR_SHA256 -and $LiteralPath -eq $env:SIYI_PROBE_SIDECAR) {
        return @{Hash = $env:SIYI_PROBE_SIDECAR_SHA256}
    }
    Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $LiteralPath -Algorithm $Algorithm
}
$sourceIdentity = @{
    source_commit = $env:SIYI_PROBE_COMMIT
    source_tree_fingerprint = $env:SIYI_PROBE_FINGERPRINT
}
$previous = @{version='8.0.1'; sha256=$env:SIYI_PROBE_MSI_SHA256}
Assert-InstalledBuildIdentity -Application (Get-Item -LiteralPath $env:SIYI_PROBE_APP) `
    -ExpectedVersion $env:SIYI_PROBE_VERSION -RequireCurrentSource ($env:SIYI_PROBE_CURRENT -eq '1') `
    -PreviousInstallerIdentity $previous | ConvertTo-Json -Compress
"""


def test_msi_diagnostics_and_precreated_database_readiness() -> None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    assert shell is not None, "Windows installer tests require PowerShell"
    script = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility')
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SIYI_MSI_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors -join '; ') }
foreach ($name in 'Write-MsiPreflightFailure','Test-MsiApplicationReadiness') {
    $function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
    if (-not $function) { throw 'MSI probe function missing' }
    . ([scriptblock]::Create($function.Extent.Text))
}
$env:SIYI_HOSTED_PREFLIGHT_TYPED_MSI = '1'
Write-MsiPreflightFailure -Stage 'launch_candidate' -ErrorMessage 'MSI-installed application has no closable main window.' -MsiExit $null -PrivilegeBlocked $false
Write-MsiPreflightFailure -Stage 'upgrade' -ErrorMessage 'private runtime error, must not appear' -MsiExit 1603 -PrivilegeBlocked $false
Write-MsiPreflightFailure -Stage 'validate_migration' -ErrorMessage 'private runtime error, must not appear' -MsiExit $null -PrivilegeBlocked $false
$env:SIYI_HOSTED_PREFLIGHT_TYPED_MSI = ''
Write-MsiPreflightFailure -Stage 'upgrade' -ErrorMessage 'disabled event' -MsiExit 1603 -PrivilegeBlocked $false
@{
    preseeded = Test-MsiApplicationReadiness $true $true $true $false ([IntPtr]::Zero)
    no_window = Test-MsiApplicationReadiness $true $true $true $true ([IntPtr]::Zero)
    ready = Test-MsiApplicationReadiness $true $true $true $true ([IntPtr]123)
    legacy = Test-MsiApplicationReadiness $true $true $false $false ([IntPtr]::Zero)
} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "SIYI_MSI_SCRIPT": str(ROOT / "scripts" / "smoke-msi.ps1")},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "private runtime error" not in result.stdout
    *diagnostics, readiness = result.stdout.strip().splitlines()
    prefix = "SIYI_MSI_PREFLIGHT_FAILURE "
    assert [json.loads(line.removeprefix(prefix)) for line in diagnostics] == [
        {"stage": "launch_candidate", "reason": "NATIVE_WINDOW_MISSING"},
        {"stage": "upgrade", "reason": "MSI_EXIT", "msi_exit": 1603},
        {"stage": "validate_migration", "reason": "LIFECYCLE_CHECK_FAILED"},
    ]
    assert all(line.startswith(prefix) for line in diagnostics)
    assert json.loads(readiness) == {"preseeded": False, "no_window": False, "ready": True, "legacy": True}


def test_failed_msi_lifecycle_cleanup_tracks_the_last_successful_installer() -> None:
    source = (ROOT / "scripts" / "smoke-msi.ps1").read_text(encoding="utf-8")
    old_install = source.index("$msiDiagnosticStage = 'install_previous'")
    old_owned = source.index("$ownedInstaller = $initialInstaller", old_install)
    upgrade = source.index("$msiDiagnosticStage = 'upgrade'", old_owned)
    new_owned = source.index("$ownedInstaller = $msi.FullName", upgrade)
    cleanup = source.index("if ($installed -and $ownedInstaller)", new_owned)
    assert old_install < old_owned < upgrade < new_owned < cleanup
    cleanup_code = source[cleanup:source.index("foreach ($temporaryPath", cleanup)]
    assert "$ownedInstaller" in cleanup_code and "$msi.FullName" not in cleanup_code


def test_msi_evidence_creates_parent_and_requires_a_real_privilege_error(tmp_path: Path) -> None:
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("MSI evidence writer requires PowerShell 7")
    normal_log = tmp_path / "normal.log"
    normal_log.write_text("Property value 1925. Installer succeeded.\n", encoding="utf-8")
    error_log = tmp_path / "error.log"
    error_log.write_text("MSI (s): Note: 1: 1925 2: example\n", encoding="utf-8")
    script = r"""
$ErrorActionPreference = 'Stop'
$root = $env:SIYI_MSI_TEST_ROOT
$Output = Join-Path $root 'fresh/nested/result.json'
$sourceIdentity = $null
$runId = 'fixture'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SIYI_MSI_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors -join '; ') }
foreach ($name in 'Resolve-EvidenceOutputPath','Copy-EvidenceLog','Test-MsiPrivilegeError') {
    $function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $false)
    if (-not $function) { throw 'MSI probe function missing' }
    . ([scriptblock]::Create($function.Extent.Text))
}
$normal = Join-Path $root 'normal.log'
$errorLog = Join-Path $root 'error.log'
$copy = Copy-EvidenceLog -LogPath $normal -Label 'msi-install'
$immutable = $false
try { Copy-EvidenceLog -LogPath $normal -Label 'msi-install' | Out-Null } catch { $immutable = $true }
@{
    copy = $copy
    immutable = $immutable
    incidental = Test-MsiPrivilegeError $normal 1603
    success = Test-MsiPrivilegeError $errorLog 0
    unrelated = Test-MsiPrivilegeError $errorLog $null
    actual_error = Test-MsiPrivilegeError $errorLog 1603
} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "SIYI_MSI_SCRIPT": str(ROOT / "scripts" / "smoke-msi.ps1"), "SIYI_MSI_TEST_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed.pop("copy") == {
        "path": "fresh/nested/msi-install-fixture.log",
        "sha256": hashlib.sha256(normal_log.read_bytes()).hexdigest().upper(),
        "bytes": normal_log.stat().st_size,
    }
    assert observed == {"immutable": True, "incidental": False, "success": False, "unrelated": False, "actual_error": True}


def build_manifest(version: str) -> bytes:
    return json.dumps(
        {
            "product_version": version,
            "git_commit": COMMIT,
            "source_fingerprint": FINGERPRINT,
            "workspace_state": "CLEAN",
            "build_id": BUILD_ID,
            "component_build_ids": {"sidecar": f"sidecar-{BUILD_ID}"},
        }
    ).encode("utf-8")


def probe(
    tmp_path: Path,
    *,
    version: str = "8.0.1",
    embedded_version: str = "8.0.1",
    msi_sha256: str = PRIOR_MSI_SHA256,
    use_published_sidecar_hash: bool = True,
    current: bool = False,
    onedir: bool = False,
) -> tuple[subprocess.CompletedProcess[str], bytes, Path]:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    assert shell is not None, "Windows installer tests require PowerShell"
    app = tmp_path / "desktop.exe"
    app.write_bytes(b"static desktop fixture; never executed")
    embedded = build_manifest(embedded_version)
    manifest = tmp_path / "fixture.json"
    manifest.write_bytes(embedded)
    sidecar = tmp_path / "agent-backend.exe"
    CArchiveWriter(str(sidecar), [("build-info.json", str(manifest), True, "x")], "python312.dll")
    if onedir:
        adjacent = tmp_path / "_internal" / "build-info.json"
        adjacent.parent.mkdir()
        adjacent.write_bytes(build_manifest(version))
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", PROBE],
        env={
            **os.environ,
            "SIYI_PROBE_ROOT": str(ROOT),
            "SIYI_PROBE_PYTHON": sys.executable,
            "SIYI_PROBE_APP": str(app),
            "SIYI_PROBE_SIDECAR": str(sidecar),
            "SIYI_PROBE_SIDECAR_SHA256": PRIOR_SIDECAR_SHA256 if use_published_sidecar_hash else "",
            "SIYI_PROBE_COMMIT": COMMIT,
            "SIYI_PROBE_FINGERPRINT": FINGERPRINT,
            "SIYI_PROBE_VERSION": version,
            "SIYI_PROBE_CURRENT": "1" if current else "0",
            "SIYI_PROBE_MSI_SHA256": msi_sha256,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, embedded, sidecar


def test_msi_prior_identity_reads_and_binds_only_embedded_sidecar(tmp_path: Path) -> None:
    result, embedded, sidecar = probe(tmp_path)
    assert result.returncode == 0, result.stderr
    identity = json.loads(result.stdout)
    assert identity["identity_mode"] == "legacy_onefile_embedded_manifest"
    assert identity["installer_sha256"] == PRIOR_MSI_SHA256
    assert identity["installer_version"] == identity["product_version"] == "8.0.1"
    assert identity["embedded_manifest_entry"] == "build-info.json"
    assert identity["embedded_manifest_bytes"] == len(embedded)
    assert identity["embedded_manifest_sha256"] == hashlib.sha256(embedded).hexdigest().upper()
    assert identity["executable_name"] == sidecar.name
    assert identity["executable_sha256"] == PRIOR_SIDECAR_SHA256


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"msi_sha256": "A" * 64}, "adjacent onedir"),
        ({"version": "8.0.2"}, "adjacent onedir"),
        ({"current": True}, "adjacent onedir"),
        ({"use_published_sidecar_hash": False}, "sidecar does not match"),
        ({"embedded_version": "8.0.2"}, "prior installer artifact"),
    ],
)
def test_msi_prior_identity_rejects_unbound_fallbacks(
    tmp_path: Path, arguments: dict[str, str | bool], message: str
) -> None:
    result, _, _ = probe(tmp_path, **arguments)
    assert result.returncode != 0
    assert message in result.stderr


def test_msi_candidate_still_requires_current_onedir_identity(tmp_path: Path) -> None:
    result, _, _ = probe(tmp_path, version="17.0.0", current=True, onedir=True)
    assert result.returncode == 0, result.stderr
    identity = json.loads(result.stdout)
    assert identity["product_version"] == "17.0.0"
    assert identity["git_commit"] == COMMIT
    assert "identity_mode" not in identity
