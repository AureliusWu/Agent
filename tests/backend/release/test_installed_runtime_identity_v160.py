import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def helper():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("installed_runtime_tested", ROOT / "scripts/read-installed-runtime.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_actual_installed_bytes_and_payload_are_observed(tmp_path):
    (tmp_path / "desktop.exe").write_bytes(b"synthetic desktop fixture")
    (tmp_path / "agent-backend.exe").write_bytes(b"synthetic backend fixture")
    (tmp_path / "_internal").mkdir()
    payload = tmp_path / "_internal/code.bin"
    payload.write_bytes(b"original code")
    first = helper().observe(tmp_path, "desktop.exe", "agent-backend.exe")
    payload.write_bytes(b"changed code")
    second = helper().observe(tmp_path, "desktop.exe", "agent-backend.exe")
    assert first["binary_sha256"] == second["binary_sha256"]
    assert first["sidecar_payload_sha256"] != second["sidecar_payload_sha256"]
    assert second["installed_sidecar_payload"]["entries"][0]["path"] == "_internal/code.bin"
    assert str(tmp_path) not in str(second)


@pytest.mark.parametrize("name", ["../desktop.exe", "C:/repo/desktop.exe", "folder\\desktop.exe", "not-executable.json"])
def test_installed_name_cannot_escape_directory(tmp_path, name):
    with pytest.raises(ValueError):
        helper().checked_file(tmp_path, name)


@pytest.mark.parametrize("script", ["smoke-installer.ps1", "smoke-msi.ps1"])
def test_installer_receipt_uses_actual_installation_before_uninstall(script):
    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert "read-installed-runtime.py') --directory $application.DirectoryName --desktop $application.Name" in source
    assert "binary_sha256 = $installedRuntimeIdentity.binary_sha256" in source
    assert "installed_sidecar_payload = $installedRuntimeIdentity.installed_sidecar_payload" in source
    assert "source_after = $installedRuntimeIdentity.source_after" in source
