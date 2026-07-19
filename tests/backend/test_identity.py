from fastapi.testclient import TestClient

from app.identity import ADMINISTRATOR_ID, AGENT_ID, _canonical_identity, active_identity, identity_system_context
from app.identity_guard import enforce_identity, inspect_identity_claim
from app.main import app


def test_identity_kernel_is_seeded_and_stable() -> None:
    identity = active_identity()

    assert identity["agent_id"] == AGENT_ID
    assert identity["identity"]["display_name"] == "夏目心"
    assert identity["identity"]["user_id"] == ADMINISTRATOR_ID
    assert "模型供应商" in identity_system_context()
    assert "司忆是承载夏目心" in identity_system_context()
    assert "基座模型只是可替换的认知引擎" in identity_system_context()
    assert "不由当前工作区决定" in identity_system_context()


def test_packaged_identity_fallback_does_not_require_external_json(monkeypatch) -> None:
    monkeypatch.setattr("app.identity._IDENTITY_PATH", type("MissingPath", (), {"is_file": lambda self: False})())
    assert _canonical_identity()["agent_id"] == AGENT_ID


def test_identity_guard_only_blocks_direct_self_identity_drift() -> None:
    assert inspect_identity_claim("我是 ChatGPT，可以帮助你。").passed is False
    assert inspect_identity_claim("DeepSeek 是当前模型供应商。").passed is True
    assert inspect_identity_claim("```python\nprint('我是 ChatGPT')\n```").passed is True
    assert "我是夏目心" in enforce_identity("我是 Claude，可以帮助你。")


def test_identity_version_requires_explicit_administrator_confirmation() -> None:
    with TestClient(app) as client:
        current = client.get("/api/identity")
        denied = client.post(
            "/api/identity/versions",
            json={
                "identity": {"display_name": "夏目心"},
                "reason": "test change",
                "administrator_confirmed": False,
            },
        )

    assert current.status_code == 200
    assert current.json()["agent_id"] == AGENT_ID
    assert denied.status_code == 403
