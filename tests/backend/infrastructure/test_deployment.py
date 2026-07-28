import pytest

from app.config import Settings
from app.deployment import DeploymentSecurityError, deployment_requires_auth, is_loopback_host, validate_deployment_security


def configuration(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_loopback_detection_and_desktop_mode() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("0.0.0.0") is False
    assert deployment_requires_auth(configuration(deployment_mode="desktop_local", bind_host="127.0.0.1")) is False


def test_desktop_mode_cannot_listen_beyond_loopback() -> None:
    config = configuration(deployment_mode="desktop_local", bind_host="0.0.0.0", api_token="configured")
    with pytest.raises(DeploymentSecurityError, match="回环"):
        validate_deployment_security(config)
