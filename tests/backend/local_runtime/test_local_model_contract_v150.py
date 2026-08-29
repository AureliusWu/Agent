from __future__ import annotations

from fastapi.testclient import TestClient

from app.local_runtime.model_manager import model_manager
from app.main import app


def test_installed_local_models_expose_v2_identity_and_selection_fields(monkeypatch) -> None:
    async def models():
        return [
            {
                "name": "llama3.2:3b",
                "size": 2_000_000_000,
                "digest": "sha256:model-digest",
                "quantization": "Q4_K_M",
                "loaded": True,
                "capabilities": {"native_tool_calls": True},
            }
        ]

    monkeypatch.setattr(model_manager, "list_models", models)

    with TestClient(app) as client:
        response = client.get("/api/local-models/models")

    assert response.status_code == 200
    assert response.json() == [
        {
            "provider": "ollama",
            "model_id": "llama3.2:3b",
            "display_name": "llama3.2:3b",
            "size": 2_000_000_000,
            "digest": "sha256:model-digest",
            "quantization": "Q4_K_M",
            "capabilities": {"native_tool_calls": True},
            "installed": True,
            "loaded": True,
            "benchmark": None,
            "name": "llama3.2:3b",
        }
    ]
