"""Offline checks only; no user application data, credentials or network."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app import tls_diagnostics


ROOT = Path(__file__).resolve().parents[3]
HOOK_PATH = ROOT / "scripts/pyinstaller-hooks/hook-ssl.py"


def load_hook():
    specification = importlib.util.spec_from_file_location("ssl_packaging_hook_tested", HOOK_PATH)
    assert specification is not None and specification.loader is not None
    result = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(result)
    return result


def fake_ssl(root: Path, *, patched: bool):
    def patched_constructor():
        return "codex-rejected-cas.pem"

    def stock_constructor():
        return None

    return SimpleNamespace(
        __file__=str(root / "ssl.py"),
        SSLContext=SimpleNamespace(__new__=patched_constructor if patched else stock_constructor),
    )


def test_stock_interpreter_does_not_collect_unreferenced_pem(tmp_path):
    hook = load_hook()
    (tmp_path / hook.COMPANION_NAME).write_bytes(b"not needed by stock Python")
    assert hook.collect_ssl_companion_data(fake_ssl(tmp_path, patched=False)) == []


def test_referenced_companion_is_collected_only_after_full_byte_validation(tmp_path, monkeypatch):
    hook = load_hook()
    content = b"synthetic public distrust record used only in this fixture"
    path = tmp_path / hook.COMPANION_NAME
    path.write_bytes(content)
    monkeypatch.setattr(hook, "COMPANION_BYTES", len(content))
    monkeypatch.setattr(hook, "COMPANION_SHA256", hashlib.sha256(content).hexdigest())
    assert hook.collect_ssl_companion_data(fake_ssl(tmp_path, patched=True)) == [(str(path), ".")]


@pytest.mark.parametrize("content", [None, b"", b"synthetic replacement", b"-----BEGIN " + b"PRIVATE KEY-----"])
def test_missing_or_unreviewed_referenced_companion_blocks_build(tmp_path, content):
    hook = load_hook()
    if content is not None:
        (tmp_path / hook.COMPANION_NAME).write_bytes(content)
    with pytest.raises(ValueError):
        hook.collect_ssl_companion_data(fake_ssl(tmp_path, patched=True))


def test_build_entries_load_hook_and_test_frozen_tls_before_runtime_or_package_use():
    for filename in ("build-runtime.ps1", "build-desktop.ps1"):
        source = (ROOT / "scripts" / filename).read_text(encoding="utf-8")
        assert "--additional-hooks-dir $hookDirectory" in source
        assert "--tls-self-check" in source
        assert "Frozen sidecar TLS self-check failed" in source
    runtime = (ROOT / "scripts/build-runtime.ps1").read_text(encoding="utf-8")
    assert runtime.index("--tls-self-check") < runtime.index("& $tauri build")
    desktop = (ROOT / "scripts/build-desktop.ps1").read_text(encoding="utf-8")
    assert desktop.index("--tls-self-check") < desktop.index("Sync-SidecarSupportDirectory (Join-Path")
    entry = (ROOT / "siyi/run_server.py").read_text(encoding="utf-8")
    assert entry.index('"--tls-self-check"') < entry.index("from app.runtime_paths")


def test_self_check_constructs_real_verified_tls_transport_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline self-check must not open a network socket")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    result = tls_diagnostics.tls_self_check()
    assert result["status"] == "passed"
    assert result["ssl_context_constructed"] is True
    assert result["httpx_transport_constructed"] is True
    assert result["certificate_verification"] is True
    assert result["hostname_verification"] is True
    assert result["network_requests"] == result["credential_reads"] == 0
    assert result["application_started"] is False


@pytest.mark.parametrize("exception,category", [
    (FileNotFoundError("synthetic private path should never be printed"), "tls_resource_missing"),
    (ssl.SSLError("synthetic private error should never be printed"), "tls_initialization_failed"),
])
def test_self_check_failure_is_explicit_but_never_includes_exception_text(monkeypatch, exception, category):
    def failed(*args, **kwargs):
        raise exception

    monkeypatch.setattr(tls_diagnostics.ssl, "create_default_context", failed)
    result = tls_diagnostics.tls_self_check()
    assert result["status"] == "failed"
    assert result["error_type"] == category
    assert "private" not in json.dumps(result)


def test_self_check_refuses_disabled_certificate_verification(monkeypatch):
    monkeypatch.setattr(tls_diagnostics.ssl, "create_default_context", lambda **kwargs: SimpleNamespace(
        verify_mode=ssl.CERT_NONE, check_hostname=False))
    result = tls_diagnostics.tls_self_check()
    assert result["status"] == "failed"
    assert result["error_type"] == "tls_verification_disabled"


def test_server_entry_self_check_exits_without_creating_application_layout(tmp_path):
    owned = tmp_path / "must-not-be-created"
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(("AGENT_", "SIYI_"))}
    environment.update({
        "AGENT_DATA_ROOT": str(owned),
        "AGENT_DESKTOP_DATA_DIRECTORY": str(owned),
        "AGENT_DATABASE_PATH": str(owned / "data/agent.db"),
        "AGENT_LOG_PATH": str(owned / "logs/agent.log"),
        "AGENT_ENV_FILE": str(owned / "config/desktop.env"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    result = subprocess.run([sys.executable, str(ROOT / "siyi/run_server.py"), "--tls-self-check"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["network_requests"] == payload["credential_reads"] == 0
    assert payload["application_started"] is False
    assert not owned.exists()
