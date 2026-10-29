from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "smoke-provider-connection.py"
SPEC = importlib.util.spec_from_file_location("provider_connection_smoke", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_smoke_environment_does_not_inherit_credentials_or_build_overrides(monkeypatch, tmp_path):
    for name in ("AGENT_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "AGENT_TAVILY_API_KEY",
                 "SIYI_BUILD_MANIFEST", "SIYI_ALLOW_PAID_API", "HTTPS_PROXY", "SSL_CERT_FILE"):
        monkeypatch.setenv(name, "not-a-real-credential")
    environment = MODULE.controlled_environment(tmp_path, 48123, "synthetic-local-token")
    assert environment["AGENT_PROVIDER_CONFIG_PATH"] == str(tmp_path / "config" / "provider.json")
    assert environment["SIYI_ALLOW_PAID_API"] == "false"
    assert environment["SIYI_TEST_PROVIDER"] == "mock"
    assert environment["AGENT_MODEL_MAX_RETRIES"] == "0"
    for name in ("AGENT_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "AGENT_TAVILY_API_KEY",
                 "SIYI_BUILD_MANIFEST", "HTTPS_PROXY", "SSL_CERT_FILE"):
        assert name not in environment


def test_transport_probe_accepts_only_expected_authentication_rejection():
    expected = {"status": "error", "error_type": "authentication", "http_status": 401, "models": []}
    assert MODULE.authentication_rejection(expected)
    for patch in ({"status": "ok"}, {"error_type": "tls_error"}, {"http_status": 403},
                  {"http_status": None}, {"models": [{"id": "not-proven"}]}):
        assert not MODULE.authentication_rejection({**expected, **patch})
