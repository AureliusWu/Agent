import pytest
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.deployment import DeploymentSecurityError, deployment_requires_auth, is_loopback_host, validate_deployment_security
from app.main import create_app


def configuration(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_loopback_detection_and_local_modes() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("0.0.0.0") is False
    assert deployment_requires_auth(configuration(deployment_mode="desktop_local", bind_host="127.0.0.1")) is False
    assert deployment_requires_auth(configuration(deployment_mode="local_web", bind_host="127.0.0.1")) is False


def test_desktop_mode_cannot_listen_beyond_loopback() -> None:
    config = configuration(deployment_mode="desktop_local", bind_host="0.0.0.0", api_token="configured")
    with pytest.raises(DeploymentSecurityError, match="回环"):
        validate_deployment_security(config)


@pytest.mark.parametrize("mode", ["web_control", "cloud_executor"])
def test_remote_modes_require_api_token(mode: str) -> None:
    config = configuration(deployment_mode=mode, bind_host="127.0.0.1", api_token="")
    with pytest.raises(DeploymentSecurityError, match="AGENT_API_TOKEN"):
        validate_deployment_security(config)


def test_non_loopback_local_web_requires_api_token() -> None:
    unsecured = configuration(deployment_mode="local_web", bind_host="0.0.0.0", api_token="")
    with pytest.raises(DeploymentSecurityError, match="AGENT_API_TOKEN"):
        validate_deployment_security(unsecured)

    secured = configuration(deployment_mode="local_web", bind_host="0.0.0.0", api_token="access-token")
    assert validate_deployment_security(secured)["authentication_required"] is True


def test_remote_api_rejects_missing_or_invalid_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "web_control")
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    monkeypatch.setattr(settings, "api_token", "remote-access-token")

    with TestClient(create_app()) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/conversations").status_code == 401
        assert client.get("/api/conversations", headers={"X-Agent-Api-Token": "wrong"}).status_code == 401
        assert client.get("/api/conversations", headers={"X-Agent-Api-Token": "remote-access-token"}).status_code == 200


def test_remote_app_refuses_to_start_without_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "web_control")
    monkeypatch.setattr(settings, "bind_host", "127.0.0.1")
    monkeypatch.setattr(settings, "api_token", "")

    with pytest.raises(DeploymentSecurityError, match="AGENT_API_TOKEN"):
        with TestClient(create_app()):
            pass
