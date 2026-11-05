"""Exercise JSON wire types against an in-process Ollama duration boundary.

No server, model or credential is used. Ollama v0.32.5's Duration accepts
negative JSON numbers; strings require a valid Go duration, so '-1' fails.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.database import connect
from app.local_runtime.model_manager import ModelManager, ModelManagerError


@pytest.mark.parametrize("selected,wire", [("-1", -1), ("0", "0"), ("5m", "5m"), ("10m", "10m")])
def test_preload_sends_a_valid_duration_and_preserves_selected_receipt(monkeypatch, selected, wire):
    manager = ModelManager("http://127.0.0.1:11435")
    loaded = False
    requests = []

    def handler(request):
        nonlocal loaded
        requests.append((request.method, request.url.path))
        assert "authorization" not in request.headers
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}] if loaded else []})
        assert request.method == "POST" and request.url.path == "/api/generate"
        payload = json.loads(request.content)
        duration = payload["keep_alive"]
        # Model the documented numeric-or-duration-string parsing boundary,
        # including the real failure that preceded any model load.
        if isinstance(duration, str) and duration not in {"0", "5m", "10m"}:
            return httpx.Response(400, json={"error": "duration requires a unit"})
        assert duration == wire and type(duration) is type(wire)
        assert payload == {"model": "qwen3:4b", "prompt": "", "stream": False, "keep_alive": wire}
        loaded = duration != "0"
        return httpx.Response(200, json={"done": True})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("app.local_runtime.model_manager.httpx.AsyncClient",
                        lambda **kwargs: original_client(transport=transport, trust_env=False, **kwargs))
    monkeypatch.setattr("app.local_runtime.model_manager.resource_coordinator.assess_admission",
                        lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(manager, "_resource_snapshot", lambda **kwargs: {})
    with connect() as db:
        before = db.execute("SELECT COUNT(*) FROM model_load_records").fetchone()[0]
    if selected == "0":
        # The original API option unloads immediately. Preserve the manager's
        # confirmation guard rather than inventing a successful resident model.
        with pytest.raises(ModelManagerError) as failure:
            asyncio.run(manager.preload("qwen3:4b", selected, require_idle_runtime=True))
        assert failure.value.code == "MODEL_LOAD_UNCONFIRMED"
        with connect() as db:
            assert db.execute("SELECT COUNT(*) FROM model_load_records").fetchone()[0] == before
        return
    receipt = asyncio.run(manager.preload("qwen3:4b", selected, require_idle_runtime=True))
    assert requests == [("GET", "/api/ps"), ("POST", "/api/generate"), ("GET", "/api/ps")]
    assert receipt["status"] == "LOADED" and receipt["keep_alive"] == selected
    with connect() as db:
        row = db.execute("SELECT status,keep_alive FROM model_load_records ORDER BY rowid DESC LIMIT 1").fetchone()
    assert tuple(row) == ("LOADED", selected)


@pytest.mark.parametrize("invalid", ["forever", "-2", "-1m", True, False, -1])
def test_preload_does_not_expand_the_public_duration_allowlist(monkeypatch, invalid):
    manager = ModelManager("http://127.0.0.1:11435")

    async def unexpected(*args, **kwargs):
        pytest.fail("Invalid duration must be rejected before contacting a runtime")

    monkeypatch.setattr(manager, "_json", unexpected)
    with pytest.raises(ModelManagerError) as failure:
        asyncio.run(manager.preload("qwen3:4b", invalid))
    assert failure.value.code == "INVALID_KEEP_ALIVE"


def test_preload_http_failure_does_not_record_a_success(monkeypatch):
    manager = ModelManager("http://127.0.0.1:11435")

    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(500, json={"error": "private server details must remain redacted"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr("app.local_runtime.model_manager.httpx.AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), trust_env=False, **kwargs))
    monkeypatch.setattr("app.local_runtime.model_manager.resource_coordinator.assess_admission",
                        lambda *args, **kwargs: {"allowed": True})
    monkeypatch.setattr(manager, "_resource_snapshot", lambda **kwargs: {})
    with connect() as db:
        before = db.execute("SELECT COUNT(*) FROM model_load_records").fetchone()[0]
    with pytest.raises(ModelManagerError) as failure:
        asyncio.run(manager.preload("qwen3:4b", "-1", require_idle_runtime=True))
    assert failure.value.code == "OLLAMA_API_ERROR"
    assert "private server details" not in str(failure.value)
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM model_load_records").fetchone()[0] == before
