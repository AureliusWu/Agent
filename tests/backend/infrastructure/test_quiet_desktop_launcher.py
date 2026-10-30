from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "启动司忆.vbs"


def test_default_entry_hides_powershell_without_bypassing_profile_checks() -> None:
    content = ENTRY.read_bytes()
    assert content.isascii(), "Windows Script Host decodes non-ASCII source using the system codepage"
    source = content.decode("ascii")
    assert 'fileSystem.GetParentFolderName(WScript.ScriptFullName)' in source
    assert 'fileSystem.BuildPath(taskRoot, "scripts\\start-local-desktop.ps1")' in source
    assert '-NoProfile -NonInteractive -WindowStyle Hidden' in source
    assert 'shell.Run(command, 0, True)' in source
    assert 'processEnvironment("PSModulePath") = ""' in source
    assert 'If exitCode <> 0 Then' in source
    assert 'MsgBox help' in source
    assert 'WScript.Echo help' in source
    assert 'WScript.Quit code' in source
    assert 'cmd /c' not in source.lower()
    assert 'start-desktop.ps1' not in source
    assert re.search(r"(?i)(?<![a-z0-9_])[a-z]:[\\/]", source) is None

    diagnostic = (ROOT / "启动司忆.cmd").read_text(encoding="utf-8")
    assert not re.search(r"(?im)^\s*pause\s*$", diagnostic)
    assert 'start-local-desktop.ps1' in diagnostic
    launcher = (ROOT / "scripts" / "start-local-desktop.ps1").read_text(encoding="utf-8")
    assert 'Get-FileHash' in launcher
    assert 'local-profile-owner.json' in launcher
    assert 'io.github.aureliuswu.agent-sim' in launcher


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Script Host startup")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_quiet_entry_uses_hidden_child_and_reports_failure_without_real_app(tmp_path: Path, exit_code: int) -> None:
    system32 = Path(os.environ.get("SystemRoot", "")) / "System32"
    cscript = system32 / "cscript.exe"
    powershell = system32 / "WindowsPowerShell/v1.0/powershell.exe"
    if not cscript.is_file() or not powershell.is_file():
        pytest.skip("Windows Script Host or Windows PowerShell 5.1 is unavailable")

    # Copy only the wrapper. The child is our inert fixture script; it never
    # loads SiYi, invokes a runtime, imports configuration or reads credentials.
    runtime = tmp_path / "带空格的 runtime"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    entry = runtime / ENTRY.name
    shutil.copyfile(ENTRY, entry)
    (scripts / "start-local-desktop.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$record = [ordered]@{ command_line = [Environment]::CommandLine; "
        "window_handle = [Diagnostics.Process]::GetCurrentProcess().MainWindowHandle.ToInt64(); "
        "working_directory = [Environment]::CurrentDirectory }\n"
        "$outputPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'stub-result.json'\n"
        "[IO.File]::WriteAllText($outputPath, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))\n"
        f"exit {exit_code}\n",
        encoding="ascii",
    )
    environment = {
        key: value for key, value in os.environ.items()
        if key.casefold() != "psmodulepath" and not key.upper().startswith(("AGENT_", "SIYI_"))
    }
    for name, relative in (("USERPROFILE", "home"), ("HOME", "home"), ("APPDATA", "roaming"), ("LOCALAPPDATA", "local"), ("TEMP", "temp"), ("TMP", "temp")):
        directory = tmp_path / relative
        directory.mkdir(exist_ok=True)
        environment[name] = str(directory)
    result = subprocess.run(
        [str(cscript), "//NoLogo", str(entry)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    record = json.loads((runtime / "stub-result.json").read_text(encoding="utf-8"))
    assert '-WindowStyle Hidden' in record["command_line"]
    assert '-NonInteractive' in record["command_line"]
    assert str(scripts / "start-local-desktop.ps1") in record["command_line"]
    assert record["window_handle"] == 0
    assert Path(record["working_directory"]) == runtime
    assert not (runtime / "local-profile-owner.json").exists()
    if exit_code:
        assert "SiYi did not start (exit 7)" in result.stdout
        assert "No existing application was stopped" in result.stdout
        assert "For details, open a terminal" in result.stdout
    else:
        assert not result.stdout.strip()


def test_v16_patch_activation_keeps_original_data_location_and_owner() -> None:
    activation = (ROOT / "scripts" / "activate-local-desktop.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "start-local-desktop.ps1").read_text(encoding="utf-8")
    assert "$taskOwner = 'siyi-user-testing-v16.0.0'" in activation
    assert "v16.0.0" in launcher
    assert "v16.0.1" not in launcher
    assert "$taskBuild.product_version -cne $taskVersion" in activation
