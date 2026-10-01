"""Offline contracts; evaluate isolated PowerShell functions, never build apps."""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/build-desktop.ps1"


def powershell(body: str, **environment: str) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable, "PowerShell is required for the Windows candidate build contract"
    prelude = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SIYI_BUILD_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | ForEach-Object Message | Out-String) }
function Load-Function([string]$name) {
    $function = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true) | Select-Object -First 1
    if (-not $function) { throw "Missing production function $name" }
    [ScriptBlock]::Create($function.Extent.Text)
}
"""
    return subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", prelude + body],
        cwd=ROOT, env={**os.environ, "SIYI_BUILD_SCRIPT": str(SCRIPT), **environment},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, check=False,
    )


def result(body: str, **environment: str):
    completed = powershell(body, **environment)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_build_script_parses_and_exposes_candidate_mode():
    parameters = result("$ast.ParamBlock.Parameters.Name.VariablePath.UserPath | ConvertTo-Json -Compress")
    assert {"CandidateOnly", "CandidateDirectory"} <= set(parameters)
    source = SCRIPT.read_text(encoding="utf-8")
    assert "frontendDist = $candidateFrontendDistRelative" in source
    assert "$candidateFrontendDistRelative = '../../'" in source
    assert "beforeBuildCommand = 'npm --prefix ../frontend run build:desktop'" in source
    assert "'SIYI_CANDIDATE_FRONTEND_DIST'" in source


@pytest.mark.parametrize("path", [".", "build", "build/candidates", "build/candidates/../escape"])
def test_candidate_path_cannot_point_to_existing_runtime_or_escape(tmp_path: Path, path: str):
    completed = powershell(r"""
. (Load-Function 'Resolve-CandidateDirectory')
Resolve-CandidateDirectory $env:SIYI_TEST_ROOT $env:SIYI_TEST_PATH '16.0.0'
""", SIYI_TEST_ROOT=str(tmp_path), SIYI_TEST_PATH=path)
    assert completed.returncode != 0
    assert "below build" in completed.stderr


def test_candidate_output_is_fresh_and_existing_results_are_retained(tmp_path: Path):
    requested = "build/candidates/v16-offline-test"
    actual = result(r"""
. (Load-Function 'Resolve-CandidateDirectory')
Resolve-CandidateDirectory $env:SIYI_TEST_ROOT $env:SIYI_TEST_PATH '16.0.0' | ConvertTo-Json -Compress
""", SIYI_TEST_ROOT=str(tmp_path), SIYI_TEST_PATH=requested)
    assert Path(actual) == tmp_path / requested
    assert not Path(actual).exists(), "path validation alone must not mutate the filesystem"
    Path(actual).mkdir(parents=True)
    marker = Path(actual) / "prior-result.txt"
    marker.write_text("retained", encoding="utf-8")
    completed = powershell(r"""
. (Load-Function 'Resolve-CandidateDirectory')
Resolve-CandidateDirectory $env:SIYI_TEST_ROOT $env:SIYI_TEST_PATH '16.0.0'
""", SIYI_TEST_ROOT=str(tmp_path), SIYI_TEST_PATH=requested)
    assert completed.returncode != 0 and "already exists" in completed.stderr
    assert marker.read_text(encoding="utf-8") == "retained"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction output contract")
def test_candidate_rejects_a_real_reparse_ancestor(tmp_path: Path):
    import _winapi

    (tmp_path / "build").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "build/candidates"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        completed = powershell(r"""
. (Load-Function 'Resolve-CandidateDirectory')
Resolve-CandidateDirectory $env:SIYI_TEST_ROOT 'build/candidates/fresh' '16.0.0'
""", SIYI_TEST_ROOT=str(tmp_path))
        assert completed.returncode != 0 and "reparse" in completed.stderr
        assert list(outside.iterdir()) == []
    finally:
        junction.rmdir()


def test_candidate_runtime_paths_are_computed_under_fresh_portable_directory(tmp_path: Path):
    candidate = tmp_path / "build/candidates/fresh"
    candidate.mkdir(parents=True)
    paths = result(r"""
. (Load-Function 'Resolve-CandidateDirectory')
$root = $env:SIYI_TEST_ROOT; $candidateRoot = $env:SIYI_TEST_CANDIDATE
$candidateVersion = '16.0.0'; $CandidateOnly = $true; $runtimeApplicationName = 'synthetic.exe'
$assignments = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -in @('$runtimeDirectory', '$runtimeApplication', '$runtimeSidecar', '$runtimeSidecarSupportDirectory')
}, $true)
$initial = $assignments | Where-Object { $_.Left.Extent.Text -eq '$runtimeDirectory' -and $_.Right.Extent.Text -eq '$root' }
. ([ScriptBlock]::Create($initial.Extent.Text))
$branch = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$CandidateOnly' -and
    $node.Clauses[0].Item2.Extent.Text.Contains('$runtimeDirectory = Join-Path')
}, $true) | Select-Object -First 1
if (-not $branch) { throw 'Missing candidate output selection' }
. ([ScriptBlock]::Create($branch.Extent.Text))
foreach ($statement in ($assignments | Where-Object { $_.Left.Extent.Text -ne '$runtimeDirectory' })) {
    . ([ScriptBlock]::Create($statement.Extent.Text))
}
@($runtimeApplication, $runtimeSidecar, $runtimeSidecarSupportDirectory) | ConvertTo-Json -Compress
""", SIYI_TEST_ROOT=str(tmp_path), SIYI_TEST_CANDIDATE=str(candidate))
    assert all(Path(path).is_relative_to(candidate / "portable") for path in paths)
    assert not (tmp_path / "synthetic.exe").exists()
    assert not (tmp_path / "agent-backend.exe").exists()
    assert not (tmp_path / "_internal").exists()


def test_candidate_returns_before_any_runtime_smoke_or_installer():
    checks = result(r"""
$branch = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$CandidateOnly' -and
    $node.Clauses[0].Item2.Extent.Text.Contains("report_type = 'desktop_candidate_build'")
}, $true) | Select-Object -First 1
if (-not $branch) { throw 'Missing candidate-only completion branch' }
$returns = $branch.FindAll({ param($node) $node -is [System.Management.Automation.Language.ReturnStatementAst] }, $true)
$smokes = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    $node.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand -and
    $node.Extent.Text -match 'smoke-(sidecar|local-runtime-sidecar|installer|msi)\.ps1'
}, $true)
@{ returns = $returns.Count; smoke_count = $smokes.Count; returns_before_smoke = @($smokes | Where-Object { $_.Extent.StartOffset -lt $branch.Extent.EndOffset }).Count -eq 0 } | ConvertTo-Json -Compress
""")
    assert checks == {"returns": 1, "smoke_count": 4, "returns_before_smoke": True}


@pytest.mark.parametrize("option", ["ReleaseEvidence", "PreviousInstaller"])
def test_candidate_rejects_release_or_installer_options_before_building(option: str):
    completed = powershell(r"""
$CandidateOnly = $true
Set-Variable -Name $env:SIYI_TEST_OPTION -Value $true
$branch = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.IfStatementAst] -and
    $node.Clauses[0].Item1.Extent.Text -eq '$CandidateOnly' -and
    $node.Clauses[0].Item2.Extent.Text.Contains('CandidateOnly cannot be combined')
}, $true) | Select-Object -First 1
if (-not $branch) { throw 'Missing candidate mode exclusion guard' }
. ([ScriptBlock]::Create($branch.Extent.Text))
""", SIYI_TEST_OPTION=option)
    assert completed.returncode != 0 and "cannot be combined" in completed.stderr


def test_candidate_reuses_cache_but_rebuilds_own_crate_after_manifest_generation():
    checks = result(r"""
$clean = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'cargo' -and
    $node.Extent.Text.StartsWith('cargo clean ')
}, $true) | Select-Object -First 1
if (-not $clean) { throw 'Missing application-cache rebuild' }
$parent = $clean.Parent
while ($parent -and $parent -isnot [System.Management.Automation.Language.IfStatementAst]) { $parent = $parent.Parent }
$manifest = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and
    $node.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Ampersand -and
    $node.Extent.Text.Contains('generate_build_info.py')
}, $true) | Select-Object -First 1
$tauri = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and $node.Extent.Text.StartsWith('& $tauri ')
}, $true) | Select-Object -First 1
@{ guarded = $parent.Clauses[0].Item1.Extent.Text -eq '$CandidateOnly';
   clean = $clean.Extent.Text;
   manifest_before_clean = $manifest.Extent.StartOffset -lt $clean.Extent.StartOffset;
   clean_before_tauri = $clean.Extent.StartOffset -lt $tauri.Extent.StartOffset } | ConvertTo-Json -Compress
""")
    assert checks["guarded"] and checks["manifest_before_clean"] and checks["clean_before_tauri"]
    assert "--package app --release" in checks["clean"]
    assert "--target-dir $desktopTargetDirectory" in checks["clean"]


def test_candidate_dependency_install_commands_are_unreachable_and_cargo_is_locked_offline():
    checks = result(r"""
$installs = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.CommandAst] -and $node.Extent.Text -match ' -m pip install '
}, $true)
$guarded = $true
foreach ($install in $installs) {
    $parent = $install.Parent
    while ($parent -and $parent -isnot [System.Management.Automation.Language.IfStatementAst]) { $parent = $parent.Parent }
    if (-not $parent -or $parent.Clauses[0].Item1.Extent.Text -ne '-not $CandidateOnly') { $guarded = $false }
}
$CandidateOnly = $true; $candidateTauriConfig = 'candidate.json'
$assign = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$tauriBuildArguments'
}, $true)
foreach ($statement in $assign) { . ([ScriptBlock]::Create($statement.Extent.Text)) }
@{ installs = $installs.Count; guarded = $guarded; args = $tauriBuildArguments } | ConvertTo-Json -Compress
""")
    assert checks["installs"] == 2 and checks["guarded"]
    assert checks["args"][-5:] == ["--no-bundle", "--ci", "--", "--locked", "--offline"]


@pytest.mark.parametrize("failure", [False, True])
def test_production_cleanup_restores_placeholder_even_on_candidate_return_or_failure(tmp_path: Path, failure: bool):
    support = tmp_path / "desktop/src-tauri/binaries/_internal"
    support.mkdir(parents=True)
    (support / "payload.txt").write_text("candidate payload", encoding="utf-8")
    (support / ".gitkeep").write_bytes(b"changed")
    actual = result(r"""
. (Load-Function 'Restore-TrackedSidecarSupportPlaceholder')
function Read-HeadBlobBytes { return ,([byte[]]@(10)) }
$root = $env:SIYI_TEST_ROOT; $targetSupportDirectory = $env:SIYI_TEST_SUPPORT
$binaryDirectory = Split-Path -Parent $targetSupportDirectory
$trackedSupportPlaceholderRelativePath = 'desktop/src-tauri/binaries/_internal/.gitkeep'
$sidecarSupportMayHaveChanged = $true
$cleanup = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.TryStatementAst] -and $node.Finally -and
    $node.Finally.Extent.Text.Contains('Restore-TrackedSidecarSupportPlaceholder')
}, $true) | Select-Object -First 1
if (-not $cleanup) { throw 'Missing sidecar placeholder finally' }
$cleanupCode = $cleanup.Finally.Statements.Extent.Text -join "`n"
function Invoke-CandidateExit {
    try { if ($env:SIYI_TEST_FAILURE -eq 'true') { throw 'synthetic build failure' }; return }
    finally { . ([ScriptBlock]::Create($cleanupCode)) }
}
try { Invoke-CandidateExit } catch { if ($_.Exception.Message -ne 'synthetic build failure') { throw } }
[System.IO.File]::ReadAllBytes((Join-Path $targetSupportDirectory '.gitkeep')) | ConvertTo-Json -Compress
""", SIYI_TEST_ROOT=str(tmp_path), SIYI_TEST_SUPPORT=str(support), SIYI_TEST_FAILURE=str(failure).lower())
    assert actual == 10
    assert (support / "payload.txt").read_text(encoding="utf-8") == "candidate payload"


def test_candidate_environment_is_restored_when_an_early_build_step_fails():
    value = result(r"""
$CandidateOnly = $true
$previousCandidateEnvironment = @{ CARGO_NET_OFFLINE = 'previous'; SIYI_BUILD_MANIFEST = $null }
$env:CARGO_NET_OFFLINE = 'true'; $env:SIYI_BUILD_MANIFEST = 'temporary'
$cleanup = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.TryStatementAst] -and $node.Finally -and
    $node.Finally.Extent.Text.Contains('$previousCandidateEnvironment.Keys')
}, $true) | Select-Object -First 1
if (-not $cleanup) { throw 'Missing candidate environment finally' }
try { throw 'synthetic pre-PyInstaller failure' }
catch { }
finally { . ([ScriptBlock]::Create(($cleanup.Finally.Statements.Extent.Text -join "`n"))) }
@{ offline = $env:CARGO_NET_OFFLINE; manifest = $env:SIYI_BUILD_MANIFEST } | ConvertTo-Json -Compress
""")
    assert value == {"offline": "previous", "manifest": None}


@pytest.mark.parametrize("installed", [True, False])
def test_existing_dependency_check_reads_lock_without_installing(tmp_path: Path, installed: bool):
    source = SCRIPT.read_text(encoding="utf-8-sig")
    program = re.search(r"\$dependencyCheck = @'\r?\n(.*?)\r?\n'@", source, re.DOTALL)
    assert program
    lock = tmp_path / "requirements.lock"
    requirement = f"packaging=={importlib.metadata.version('packaging')}" if installed else "siyi-synthetic-nonexistent-dependency==1.0.0"
    lock.write_text("# synthetic metadata check\n" + requirement + "\n", encoding="utf-8")
    completed = subprocess.run([sys.executable, "-c", program.group(1), str(lock), str(ROOT / "siyi")],
                               capture_output=True, text=True, timeout=15, check=False)
    assert (completed.returncode == 0) == installed, completed.stderr
    assert lock.read_text(encoding="utf-8").endswith(requirement + "\n")
