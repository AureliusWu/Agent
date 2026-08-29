from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin_action_grants import MANAGEMENT_OPERATIONS
from app.database import connect, now_iso
from app.main import app
from app.permissions import authorize


def _conversation(workspace: Path, mode: str = "ask") -> int:
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("admin-action", str(workspace), mode, stamp, stamp),
        )
    return int(cursor.lastrowid)


def _grant(
    client: TestClient,
    *,
    conversation_id: int,
    operation: str,
    target_id: str,
    payload: dict,
    ui_session_id: str = "ui-session-v15",
    confirmed: bool = True,
):
    return client.post(
        "/api/admin-actions/grants",
        json={
            "operation": operation,
            "target_id": target_id,
            "payload": payload,
            "ui_session_id": ui_session_id,
            "conversation_id": conversation_id,
            "administrator_confirmed": confirmed,
        },
    )


def _headers(grant_token: str, conversation_id: int, ui_session_id: str = "ui-session-v15") -> dict[str, str]:
    return {
        "X-Siyi-Admin-Grant": grant_token,
        "X-Siyi-UI-Session": ui_session_id,
        "X-Siyi-Conversation-Id": str(conversation_id),
    }


def test_management_operation_registry_covers_every_v15_admin_action() -> None:
    assert {
        "mcp.register",
        "mcp.enable",
        "mcp.disable",
        "mcp.delete",
        "mcp.test",
        "extension.install",
        "extension.enable",
        "extension.disable",
        "extension.uninstall",
        "skill.install",
        "skill.enable",
        "skill.disable",
    }.issubset(MANAGEMENT_OPERATIONS)


def test_readonly_hard_denies_nonlow_or_nonbuiltin_permissions(tmp_path: Path) -> None:
    write = authorize(
        mode="readonly",
        risk="medium",
        tool="write_file",
        arguments={"path": "blocked.txt"},
        workspace=str(tmp_path),
    )
    external_read = authorize(
        mode="readonly",
        risk="low",
        tool="mcp__1__list",
        arguments={},
        source="mcp",
        workspace=str(tmp_path),
    )
    builtin_read = authorize(
        mode="readonly",
        risk="low",
        tool="read_file",
        arguments={"path": "README.md"},
        workspace=str(tmp_path),
    )

    assert write.allowed is False
    assert write.confirmation["error_code"] == "read_only_mode"
    assert external_read.allowed is False
    assert external_read.confirmation["error_code"] == "read_only_mode"
    assert builtin_read.allowed is True


def test_management_grant_requires_explicit_admin_confirmation_and_non_readonly_mode(tmp_path: Path) -> None:
    payload = {"name": "sample", "transport": "stdio", "url": None, "command": "sample", "args": []}

    with TestClient(app) as client:
        # Create after application startup so this test isolates the permission
        # boundary from the legacy startup migration fixed by the database task.
        ask_conversation = _conversation(tmp_path, "ask")
        readonly_conversation = _conversation(tmp_path, "readonly")
        unconfirmed = _grant(
            client,
            conversation_id=ask_conversation,
            operation="mcp.register",
            target_id="new",
            payload=payload,
            confirmed=False,
        )
        readonly = _grant(
            client,
            conversation_id=readonly_conversation,
            operation="mcp.register",
            target_id="new",
            payload=payload,
        )

    assert unconfirmed.status_code == 403
    assert readonly.status_code == 403
    assert "read_only_mode" in readonly.json()["detail"]


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("post", "/api/extensions/packages", {"workspace": "C:/workspace", "source_path": "package", "enable": True}),
        ("patch", "/api/extensions/packages/demo/1.0.0/enabled", {"enabled": True}),
        ("post", "/api/extensions/packages/demo/rollback", None),
        ("delete", "/api/extensions/packages/demo/1.0.0", None),
        ("post", "/api/skills?workspace=C%3A%2Fworkspace&name=demo&content=hello", None),
        ("patch", "/api/skills/enabled?workspace=C%3A%2Fworkspace&path=demo", {"enabled": True}),
        ("delete", "/api/skills?workspace=C%3A%2Fworkspace&path=demo", None),
        ("post", "/api/mcp", {"name": "demo", "transport": "stdio", "command": "demo", "args": []}),
        ("patch", "/api/mcp/987654/enabled", {"enabled": True}),
        ("delete", "/api/mcp/987654", None),
        ("post", "/api/mcp/987654/test", None),
    ],
)
def test_management_mutations_fail_closed_without_admin_grant(method: str, url: str, body: dict | None) -> None:
    with TestClient(app) as client:
        response = client.request(method, url, json=body)

    assert response.status_code == 403
    assert "Administrator action grant" in response.json()["detail"]


def test_extension_install_grant_is_payload_bound_and_single_use(tmp_path: Path, monkeypatch) -> None:
    conversation_id = _conversation(tmp_path)
    payload = {"workspace": str(tmp_path), "source_path": "signed-package", "enable": True}
    calls: list[tuple[str, str, bool]] = []

    def fake_install(workspace: str, source_path: str, *, enable: bool) -> dict:
        calls.append((workspace, source_path, enable))
        return {
            "extension_id": "sample.extension",
            "version": "1.0.0",
            "digest": "abc",
            "enabled": enable,
        }

    monkeypatch.setattr("app.api.routes.extensions.install_extension", fake_install)
    with TestClient(app) as client:
        granted = _grant(
            client,
            conversation_id=conversation_id,
            operation="extension.install",
            target_id="new",
            payload=payload,
        )
        assert granted.status_code == 200
        headers = _headers(granted.json()["grant_token"], conversation_id)
        installed = client.post("/api/extensions/packages", json=payload, headers=headers)
        replayed = client.post("/api/extensions/packages", json=payload, headers=headers)

        mismatch_grant = _grant(
            client,
            conversation_id=conversation_id,
            operation="extension.install",
            target_id="new",
            payload=payload,
        ).json()
        mismatched = client.post(
            "/api/extensions/packages",
            json={**payload, "source_path": "other-package"},
            headers=_headers(mismatch_grant["grant_token"], conversation_id),
        )

    assert installed.status_code == 200
    assert replayed.status_code == 403
    assert mismatched.status_code == 403
    assert calls == [(str(tmp_path), "signed-package", True)]


def test_enable_grant_cannot_authorize_disable(tmp_path: Path, monkeypatch) -> None:
    conversation_id = _conversation(tmp_path)
    calls: list[bool] = []

    def fake_set_enabled(extension_id: str, version: str, enabled: bool) -> dict:
        calls.append(enabled)
        return {"extension_id": extension_id, "version": version, "enabled": enabled}

    monkeypatch.setattr("app.api.routes.extensions.set_extension_enabled", fake_set_enabled)
    with TestClient(app) as client:
        granted = _grant(
            client,
            conversation_id=conversation_id,
            operation="extension.enable",
            target_id="demo@1.0.0",
            payload={"enabled": True},
        ).json()
        denied = client.patch(
            "/api/extensions/packages/demo/1.0.0/enabled",
            json={"enabled": False},
            headers=_headers(granted["grant_token"], conversation_id),
        )

    assert denied.status_code == 403
    assert calls == []


def test_readonly_transition_blocks_an_already_issued_management_grant(tmp_path: Path, monkeypatch) -> None:
    conversation_id = _conversation(tmp_path, "ask")
    payload = {"workspace": str(tmp_path), "source_path": "package", "enable": True}
    monkeypatch.setattr(
        "app.api.routes.extensions.install_extension",
        lambda workspace, source_path, *, enable: {
            "extension_id": "sample.extension",
            "version": "1.0.0",
            "digest": "abc",
            "enabled": enable,
        },
    )

    with TestClient(app) as client:
        granted = _grant(
            client,
            conversation_id=conversation_id,
            operation="extension.install",
            target_id="new",
            payload=payload,
        ).json()
        with connect() as db:
            db.execute("UPDATE conversations SET permission_mode='readonly' WHERE id=?", (conversation_id,))
        denied = client.post(
            "/api/extensions/packages",
            json=payload,
            headers=_headers(granted["grant_token"], conversation_id),
        )
        with connect() as db:
            db.execute("UPDATE conversations SET permission_mode='ask' WHERE id=?", (conversation_id,))
        allowed = client.post(
            "/api/extensions/packages",
            json=payload,
            headers=_headers(granted["grant_token"], conversation_id),
        )

    assert denied.status_code == 403
    assert "read_only_mode" in denied.json()["detail"]
    assert allowed.status_code == 200


def test_readonly_blocks_extension_uninstall_before_package_lookup(tmp_path: Path) -> None:
    conversation_id = _conversation(tmp_path, "ask")
    with TestClient(app) as client:
        granted = _grant(
            client,
            conversation_id=conversation_id,
            operation="extension.uninstall",
            target_id="missing.extension@1.0.0",
            payload={},
        ).json()
        with connect() as db:
            db.execute("UPDATE conversations SET permission_mode='readonly' WHERE id=?", (conversation_id,))
        denied = client.delete(
            "/api/extensions/packages/missing.extension/1.0.0",
            headers=_headers(granted["grant_token"], conversation_id),
        )

    assert denied.status_code == 403
    assert "read_only_mode" in denied.json()["detail"]
