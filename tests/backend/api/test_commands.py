from fastapi.testclient import TestClient

from app.main import app
from app.database import rows


def test_command_catalog_is_the_complete_controlled_registry() -> None:
    with TestClient(app) as client:
        response = client.get("/api/commands")

    assert response.status_code == 200
    commands = response.json()["commands"]
    assert [item["name"] for item in commands] == [
        "/help", "/clear", "/compact", "/context", "/cost", "/doctor", "/memory", "/search", "/stop"
    ]
    assert next(item for item in commands if item["name"] == "/clear")["risk"] == "confirm"
    assert next(item for item in commands if item["name"] == "/stop")["allowed_while_busy"] is True
    assert set(commands[0]) == {"name", "title", "description", "usage", "category", "execution", "risk", "requires_argument", "requires_conversation", "requires_workspace", "allowed_while_busy", "accepts_arguments"}


def test_command_validation_enforces_arguments_state_and_confirmation() -> None:
    with TestClient(app) as client:
        missing = client.post("/api/commands/validate", json={"text": "/memory", "has_conversation": True, "running": False})
        running = client.post("/api/commands/validate", json={"text": "/clear", "has_conversation": True, "running": True})
        confirmation = client.post("/api/commands/validate", json={"text": "/clear", "has_conversation": True, "running": False})
        ready = client.post("/api/commands/validate", json={"text": "/clear", "has_conversation": True, "running": False, "confirmed": True})
        unknown = client.post("/api/commands/validate", json={"text": "/made-up", "has_conversation": True, "running": False})

    assert missing.json()["code"] == "argument_required"
    assert running.json()["code"] == "not_allowed_while_running"
    assert confirmation.json()["status"] == "confirmation_required"
    assert ready.json()["status"] == "ready"
    assert unknown.json()["code"] == "unknown_command"


def test_stop_requires_a_real_running_task_and_local_reads_are_allowed_while_running() -> None:
    with TestClient(app) as client:
        idle_stop = client.post("/api/commands/validate", json={"text": "/stop", "running": False})
        running_stop = client.post("/api/commands/validate", json={"text": "/stop", "running": True})
        memory = client.post("/api/commands/validate", json={"text": "/memory project", "running": True})

    assert idle_stop.json()["code"] == "task_not_running"
    assert running_stop.json()["status"] == "ready"
    assert memory.json()["status"] == "ready"


def test_context_mutation_is_blocked_during_confirmation_or_recovery_and_audit_has_no_arguments() -> None:
    with TestClient(app) as client:
        waiting = client.post("/api/commands/validate", json={"text": "/clear", "has_conversation": True, "waiting_confirmation": True})
        recovering = client.post("/api/commands/validate", json={"text": "/compact", "has_conversation": True, "recovering": True})
        audited = client.post("/api/commands/audit", json={"command": "/memory", "status": "ok", "conversation_id": None})
    assert waiting.json()["code"] == "confirmation_pending"
    assert recovering.json()["code"] == "recovery_active"
    assert audited.status_code == 204
    record = rows("SELECT target,details FROM audit_logs WHERE action='local_command' ORDER BY id DESC LIMIT 1")[0]
    assert record["target"] == "/memory"
    assert "arguments_recorded" in record["details"]
    assert "关键词" not in record["details"]
