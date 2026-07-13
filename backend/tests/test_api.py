from pathlib import Path
import uuid

from fastapi.testclient import TestClient

from app.main import app
from app.database import connect, now_iso


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tauri_origin_is_allowed() -> None:
    with TestClient(app) as client:
        response = client.options("/api/conversations", headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_create_conversation_and_list_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("test", encoding="utf-8")
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"})
        assert created.status_code == 200
        result = client.post("/api/tools/execute", json={
            "conversation_id": created.json()["id"], "workspace": str(tmp_path),
            "permission_mode": "ask", "tool": "list_files", "arguments": {"path": "."},
        })
    assert result.status_code == 200
    assert result.json()["items"][0]["name"] == "README.md"


def test_context_stats_endpoint(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"})
        response = client.get(f"/api/conversations/{created.json()['id']}/context")
    assert response.status_code == 200
    assert response.json()["message_count"] == 0


def test_permission_mode_is_persisted(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        changed = client.patch(f"/api/conversations/{created['id']}/permission", json={"permission_mode": "full"})
        conversations = client.get("/api/conversations").json()
    assert changed.status_code == 200
    assert next(item for item in conversations if item["id"] == created["id"])["permission_mode"] == "full"


def test_conversation_can_be_renamed_and_deleted(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        renamed = client.patch(f"/api/conversations/{created['id']}", json={"title": "新标题"})
        deleted = client.delete(f"/api/conversations/{created['id']}")
    assert renamed.json()["title"] == "新标题"
    assert deleted.json()["deleted"] is True


def test_agent_loop_completes_and_records_task(tmp_path: Path, monkeypatch) -> None:
    async def fake_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "完成"}
    monkeypatch.setattr("app.main.completion", fake_completion)
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        response = client.post("/api/chat", json={"conversation_id": conversation["id"], "content": "回答我", "task_id": uuid.uuid4().hex})
        tasks = client.get(f"/api/conversations/{conversation['id']}/tasks").json()
    assert response.status_code == 200
    assert response.json()["task_status"] == "completed"
    assert tasks[0]["status"] == "completed"


def test_agent_loop_stops_repeated_tool_calls(tmp_path: Path, monkeypatch) -> None:
    async def repeated_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}
    monkeypatch.setattr("app.main.completion", repeated_completion)
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        response = client.post("/api/chat", json={"conversation_id": conversation["id"], "content": "循环", "task_id": uuid.uuid4().hex})
    assert response.status_code == 200
    assert response.json()["task_status"] == "partially_completed"
    assert "重复工具调用" in response.json()["content"]


def test_cancel_task_endpoint(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        with connect() as db:
            db.execute("INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation["id"], "running", "test", now_iso(), now_iso()))
        response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.json()["status"] == "cancelled"


def test_database_backup_can_be_created() -> None:
    with TestClient(app) as client:
        response = client.post("/api/database/backups")
        backups = client.get("/api/database/backups").json()
    assert response.status_code == 200
    assert any(item["name"] == response.json()["name"] for item in backups)
