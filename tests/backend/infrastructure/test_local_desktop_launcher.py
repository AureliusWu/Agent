from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "start-local-desktop.ps1"
COMMIT = "6ca33b863d5064413ba5b3639012a65c5d63fe6e"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop launcher")


@pytest.fixture
def activated_runtime(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "runtime"
    (app / "scripts").mkdir(parents=True)
    (app / "_internal").mkdir()
    shutil.copyfile(SCRIPT, app / "scripts" / SCRIPT.name)
    (app / "VERSION").write_text("16.0.0\n", encoding="utf-8")
    (app / "司忆.exe").write_bytes(b"not an executable; CheckOnly must never launch it")
    (app / "agent-backend.exe").write_bytes(b"not an executable; must never launch it")
    (app / "_internal" / "build-info.json").write_text(
        json.dumps({"product_version": "16.0.0", "git_commit": COMMIT}), encoding="utf-8"
    )
    files = []
    for relative in ("司忆.exe", "agent-backend.exe", "_internal/build-info.json"):
        path = app / relative
        files.append(
            {"path": relative, "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    (app / ".siyi-desktop.local.json").write_text(
        json.dumps(
            {"schema_version": 1, "product_version": "16.0.0", "runtime_commit": COMMIT,
             "data_owner": "test-local-v16", "files": files}
        ), encoding="utf-8"
    )
    return app, tmp_path / "owned-profile"


def check_only(app: Path, data: Path) -> subprocess.CompletedProcess[str]:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    environment = dict(os.environ)
    for key in list(environment):
        if key.casefold() == "psmodulepath":
            environment.pop(key)
    return subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(app / "scripts" / SCRIPT.name), "-DataDirectory", str(data), "-CheckOnly"],
        env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        check=False,
    )


def test_check_only_valid_runtime_does_not_create_or_launch(activated_runtime: tuple[Path, Path]) -> None:
    app, data = activated_runtime
    result = check_only(app, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "READY: 3 activated runtime files" in result.stdout
    assert "created no files and launched no process" in result.stdout
    assert not data.exists()
    assert not list(data.parent.glob("owned-profile.initializing-*"))


def test_check_only_rejects_modified_binary(activated_runtime: tuple[Path, Path]) -> None:
    app, data = activated_runtime
    (app / "agent-backend.exe").write_bytes(b"changed")
    result = check_only(app, data)
    assert result.returncode != 0
    assert "Activated runtime file changed" in result.stderr
    assert not data.exists()


def test_check_only_rejects_source_version_drift(activated_runtime: tuple[Path, Path]) -> None:
    app, data = activated_runtime
    (app / "VERSION").write_text("15.0.0", encoding="utf-8")
    result = check_only(app, data)
    assert result.returncode != 0
    assert "Source VERSION differs" in result.stderr
    assert not data.exists()


def test_check_only_refuses_unknown_existing_profile(activated_runtime: tuple[Path, Path]) -> None:
    app, data = activated_runtime
    data.mkdir()
    sentinel = data / "user-data.txt"
    sentinel.write_text("must remain unchanged", encoding="utf-8")
    result = check_only(app, data)
    assert result.returncode != 0
    assert "no local v16 owner" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "must remain unchanged"
    assert len(list(data.iterdir())) == 1


def test_check_only_refuses_wrong_profile_owner(activated_runtime: tuple[Path, Path]) -> None:
    app, data = activated_runtime
    data.mkdir()
    owner = data / "local-profile-owner.json"
    original = json.dumps({"schema_version": 1, "owner": "someone-else", "data_root": str(data)})
    owner.write_text(original, encoding="utf-8")
    result = check_only(app, data)
    assert result.returncode != 0
    assert "owner differs" in result.stderr
    assert owner.read_text(encoding="utf-8") == original
