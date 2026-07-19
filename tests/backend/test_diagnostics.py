from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.database import audit
from app.main import app


def test_diagnostic_export_redacts_credentials_and_excludes_workspace_data(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "agent.db"
    log = tmp_path / "agent.log"
    secret = "sk-test_DO_NOT_USE_000000000000"
    workspace_marker = "private-workspace-file-content"
    local_path = r"C:\Users\private\workspace\secret.txt"
    (tmp_path / "workspace.txt").write_text(workspace_marker, encoding="utf-8")
    log.write_text(f"provider failed api_key={secret}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "database_path", database)
    monkeypatch.setattr(settings, "log_path", log)
    monkeypatch.setattr(settings, "deepseek_api_key", secret)
    monkeypatch.setattr(settings, "model_base_url", "https://provider-user:provider-password@private.example/v1")

    with TestClient(app) as client:
        audit(None, "diagnostic_test", local_path, "error", {"token": secret, "message": "provider unavailable"})
        response = client.post("/api/diagnostics/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        names = set(bundle.namelist())
        content = b"\n".join(bundle.read(name) for name in names).decode("utf-8")
    assert {"manifest.json", "audit-recent.json", "data-flows-recent.json", "agent.log"} <= names
    assert secret not in content
    assert "provider-password" not in content
    assert "private.example" in content
    assert "***REDACTED***" in content
    assert "configured" in content
    assert workspace_marker not in content
    assert local_path not in content
    assert "[LOCAL_PATH]" in content
    assert not any(name.endswith((".db", ".env")) for name in names)
