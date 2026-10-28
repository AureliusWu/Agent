from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
ACTIVATE = ROOT / "scripts" / "activate-local-desktop.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows local desktop activation")


def _powershell_cases() -> list:
    windows = Path(os.environ.get("SystemRoot", "")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    pwsh = shutil.which("pwsh")
    return [
        pytest.param(str(windows), id="windows-powershell-5", marks=pytest.mark.skipif(not windows.is_file(), reason="Windows PowerShell is unavailable")),
        pytest.param(pwsh, id="powershell-7", marks=pytest.mark.skipif(not pwsh, reason="PowerShell 7 is unavailable")),
    ]


@pytest.mark.parametrize("powershell", _powershell_cases())
def test_reactivation_replaces_manifest_preserves_owner_and_archives_original(tmp_path: Path, powershell: str) -> None:
    runtime = tmp_path / "synthetic-runtime"
    (runtime / "scripts").mkdir(parents=True)
    (runtime / "_internal").mkdir()
    shutil.copyfile(ACTIVATE, runtime / "scripts" / ACTIVATE.name)
    (runtime / "VERSION").write_text("16.0.0\n", encoding="utf-8")
    (runtime / "司忆.exe").write_bytes(b"synthetic non-executable desktop version one")
    (runtime / "agent-backend.exe").write_bytes(b"synthetic non-executable sidecar")
    build_info = runtime / "_internal" / "build-info.json"
    build_info.write_text(json.dumps({"product_version": "16.0.0", "git_commit": "a" * 40, "build_id": "synthetic-one"}), encoding="utf-8")

    environment = dict(os.environ)
    for key in list(environment):
        if key.casefold() == "psmodulepath" or key.upper().startswith(("AGENT_", "SIYI_")):
            environment.pop(key)
    for variable, relative in (("USERPROFILE", "home"), ("HOME", "home"), ("APPDATA", "roaming"), ("LOCALAPPDATA", "local"), ("TEMP", "temp"), ("TMP", "temp")):
        directory = tmp_path / relative
        directory.mkdir(exist_ok=True)
        environment[variable] = str(directory)

    def activate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runtime / "scripts" / ACTIVATE.name)],
            cwd=runtime, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
        )

    first = activate()
    assert first.returncode == 0, first.stdout + first.stderr
    manifest = runtime / ".siyi-desktop.local.json"
    original_bytes = manifest.read_bytes()
    original = json.loads(original_bytes)
    assert original["data_owner"] == "siyi-user-testing-v16.0.0"
    assert original["runtime_commit"] == "a" * 40
    assert len(original["files"]) == 3
    assert not list(runtime.glob(".siyi-desktop.local.previous-*.json"))

    (runtime / "司忆.exe").write_bytes(b"synthetic non-executable desktop version two")
    build_info.write_text(json.dumps({"product_version": "16.0.0", "git_commit": "b" * 40, "build_id": "synthetic-two"}), encoding="utf-8")
    second = activate()
    assert second.returncode == 0, second.stdout + second.stderr
    current = json.loads(manifest.read_bytes())
    assert current["data_owner"] == original["data_owner"]
    assert current["runtime_commit"] == "b" * 40
    assert current["build_id"] == "synthetic-two"
    assert manifest.read_bytes() != original_bytes
    for entry in current["files"]:
        source = runtime / entry["path"]
        assert entry["bytes"] == source.stat().st_size
        assert entry["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    previous = list(runtime.glob(".siyi-desktop.local.previous-*.json"))
    assert len(previous) == 1
    assert previous[0].read_bytes() == original_bytes
    assert not list(runtime.glob(".siyi-desktop.local.json.tmp-*"))
    assert "No application, model, installer or paid validation was executed." in second.stdout
