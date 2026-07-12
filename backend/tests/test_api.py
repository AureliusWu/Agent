from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


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
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "confirm"})
        assert created.status_code == 200
        result = client.post("/api/tools/execute", json={
            "conversation_id": created.json()["id"], "workspace": str(tmp_path),
            "permission_mode": "confirm", "tool": "list_files", "arguments": {"path": "."},
        })
    assert result.status_code == 200
    assert result.json()["items"][0]["name"] == "README.md"


def test_context_stats_endpoint(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "confirm"})
        response = client.get(f"/api/conversations/{created.json()['id']}/context")
    assert response.status_code == 200
    assert response.json()["message_count"] == 0
