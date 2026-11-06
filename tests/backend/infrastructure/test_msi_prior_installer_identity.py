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
