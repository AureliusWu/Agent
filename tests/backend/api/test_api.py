from pathlib import Path
import uuid

from fastapi.testclient import TestClient

from app import __version__, create_app
from app.main import app
from app.database import connect, now_iso, record_model_run
from app.config import settings
from app.runtime.queue_service import enqueue
from app.runtime.recovery import create_checkpoint


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["build"]["component"] == "sidecar"
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["kernel"]["contract_version"] == "1.2"
        assert payload["kernel"]["services"]["executor"] == "LocalWindowsExecutor"
        assert payload["kernel"]["extension_replaceable"] is False


def test_unified_diagnostic_status_contains_build_and_schema() -> None:
    with TestClient(app) as client:
        response = client.get("/api/diagnostics/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["build"]["product_version"] == __version__
    assert payload["build"]["component_build_id"]
    assert payload["build"]["release_status"]["source_version"] == __version__
    assert payload["build"]["release_status"]["test_status"]
    assert payload["build"]["evidence_manifest_hash"]
    assert payload["database"]["schema_version"] == payload["build"]["database_schema_version"]


def test_process_api_token_protects_non_health_routes(monkeypatch) -> None:
    monkeypatch.setattr(settings, "api_token", "round13-test-token")
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/conversations").status_code == 401
        accepted = client.get("/api/conversations", headers={"X-Agent-Api-Token": "round13-test-token"})
    assert accepted.status_code == 200


def test_model_policy_exposes_routes_and_budget_without_credentials() -> None:
    with TestClient(app) as client:
        response = client.get("/api/provider/policy")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["models"]) == {"light", "medium", "strong"}
    assert payload["provider"]["name"] == "DeepSeek"
    assert payload["provider"]["api_format"] == "OpenAI-compatible"
    assert payload["provider"]["request_url"] == "https://api.deepseek.com"
    assert payload["provider"]["default_model"] == "deepseek-v4-flash"
    assert set(payload["provider"]["models"]) == {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert payload["budgets"]["task_tokens"] > 0
    assert payload["multi_agent"]["max_children"] >= 1
    assert "deepseek_api_key" not in str(payload)
    assert "sk-" not in str(payload)


def test_model_policy_previews_unsaved_ollama_selection(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    with TestClient(app) as client:
        response = client.get("/api/provider/policy?provider_id=ollama")
        rejected = client.get("/api/provider/policy?provider_id=untrusted")

    assert response.status_code == 200
    provider = response.json()["provider"]
    assert provider["id"] == "ollama"
    assert provider["default_model"] == "qwen3:4b"
    assert provider["request_url"] == "http://127.0.0.1:11434"
    assert rejected.status_code == 400


def test_provider_configuration_api_enforces_local_ollama_boundary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENT_PROVIDER_CONFIG_PATH", str(tmp_path / "provider.json"))
    with TestClient(app) as client:
        initial = client.get("/api/provider/configuration")
        assert initial.status_code == 200
        assert initial.json()["provider_id"] == "deepseek"

        rejected = client.put(
            "/api/provider/configuration",
            json={
                "provider_id": "ollama",
                "base_url": "http://192.168.1.10:11434",
                "model": "qwen3:4b",
            },
        )
        assert rejected.status_code == 400

        accepted = client.put(
            "/api/provider/configuration",
            json={
                "provider_id": "ollama",
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3:4b",
                "timeout_seconds": 120,
                "max_tokens": 4096,
                "allow_tools": True,
                "allow_streaming": True,
            },
        )
        assert accepted.status_code == 200
        assert accepted.json()["provider_id"] == "ollama"
        assert "api_key" not in (tmp_path / "provider.json").read_text(encoding="utf-8")


def test_runtime_capabilities_do_not_claim_workspace_or_remote_tools_without_evidence(tmp_path: Path) -> None:
    with TestClient(app) as client:
        without_workspace = client.get("/api/capabilities/runtime").json()
        with_workspace = client.get(f"/api/capabilities/runtime?workspace={tmp_path}").json()

    by_id = {item["id"]: item for item in without_workspace["capabilities"]}
    assert by_id["conversation"]["status"] == "available"
    assert by_id["workspace_tools"]["status"] == "unavailable"
    assert by_id["remote_mcp"]["status"] == "unconfigured"
    assert {item["id"]: item for item in with_workspace["capabilities"]}["workspace_tools"]["status"] == "available"


def test_mcp_requires_real_tool_discovery_before_enable(monkeypatch) -> None:
    async def allowed_url(*_args, **_kwargs):
        return {"url": "https://mcp.example.com/mcp", "host": "mcp.example.com"}

    async def failed_discovery(_servers, _allow_local):
        raise RuntimeError("temporary discovery failure api_key=sk-test_DO_NOT_USE_000000000000")

    async def healthy_discovery(servers, _allow_local):
        server_id = servers[0]["id"]
        tool_name = f"mcp__{server_id}__search"
        return ([{"type": "function", "function": {"name": tool_name, "description": "search", "parameters": {"type": "object", "properties": {}}}}], {tool_name: {"server_id": server_id, "method": "search"}})

    monkeypatch.setattr("app.api.routes.extensions.validate_outbound_url", allowed_url)
    monkeypatch.setattr("app.api.routes.extensions.discover_mcp_tools", failed_discovery)
    with TestClient(app) as client:
        created = client.post("/api/mcp", json={"name": "test-search-capability", "transport": "http", "url": "https://mcp.example.com/mcp", "args": []})
        assert created.status_code == 200
        item = created.json()
        assert item["enabled"] is False
        assert item["health_status"] == "error"
        assert "sk-" not in item["last_error"]
        assert "args" not in item and "command" not in item
        blocked = client.patch(f"/api/mcp/{item['id']}/enabled", json={"enabled": True})
        assert blocked.status_code == 409

        monkeypatch.setattr("app.api.routes.extensions.discover_mcp_tools", healthy_discovery)
        enabled = client.patch(f"/api/mcp/{item['id']}/enabled", json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["health_status"] == "healthy"
        assert enabled.json()["tool_count"] == 1
        runtime = client.get("/api/capabilities/runtime").json()
        assert runtime["mcp"]["available"] == 1
        client.delete(f"/api/mcp/{item['id']}")


def test_recent_tasks_reports_model_cost_by_phase(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, phase_tokens, model_route, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "completed", "cost trace", '{"analysis":30}', '{"tier":"medium"}', stamp, stamp),
            )
        record_model_run(
            conversation_id=conversation["id"], task_id=task_id, provider="test", model="test-model",
            started_at=stamp, duration_ms=12, usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
            success=True, error_type=None, retry_count=0, phase="analysis", route_tier="medium",
            task_type="general", route_confidence=0.8, max_output_tokens=100, estimated_cost_usd=0.001,
        )
        response = client.get("/api/tasks/recent")

    task = next(item for item in response.json() if item["id"] == task_id)
    assert task["phase_tokens"] == {"analysis": 30}
    assert task["phase_costs"]["analysis"] == {"calls": 1, "tokens": 30, "duration_ms": 12, "estimated_cost_usd": 0.001}


def test_usage_summary_reports_provider_cache_tokens(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation["id"], "completed", "cache trace", stamp, stamp),
            )
        record_model_run(
            conversation_id=conversation["id"],
            task_id=task_id,
            provider="test-cache",
            model="test-cache-model",
            started_at=stamp,
            duration_ms=10,
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_cache_hit_tokens": 75,
                "prompt_cache_miss_tokens": 25,
            },
            success=True,
            error_type=None,
            retry_count=0,
        )
        summary = client.get("/api/usage/summary?days=1").json()

    model = next(item for item in summary["models"] if item["model"] == "test-cache-model")
    assert model["cached_input_tokens"] == 75
    assert model["uncached_input_tokens"] == 25
    assert summary["totals"]["cached_input_tokens"] >= 75


def test_package_exports_application_factory() -> None:
    isolated = create_app()
    assert isolated.title == "Agent API"
    assert isolated.version == __version__


def test_professional_agent_profile_selection_is_preserved(tmp_path: Path) -> None:
    with TestClient(app) as client:
        catalog = client.get("/api/agent-profiles")
        created = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "ask", "agent_profile_id": "coding"},
        )
        changed = client.patch(
            f"/api/conversations/{created.json()['id']}/profile",
            json={"agent_profile_id": "data"},
        )
        conversations = client.get("/api/conversations")

    assert catalog.status_code == 200
    assert {"general", "coding", "data", "documents", "file_organizer"} <= {item["id"] for item in catalog.json()}
    assert created.status_code == 200
    assert created.json()["agent_profile_id"] == "coding"
    assert changed.status_code == 200
    stored = next(item for item in conversations.json() if item["id"] == created.json()["id"])
    assert stored["agent_profile_id"] == "data"


def test_builtin_profile_default_permission_applies_only_when_mode_is_omitted(tmp_path: Path) -> None:
    with TestClient(app) as client:
        profile_default = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "agent_profile_id": "coding"},
        )
        explicit_mode = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "agent_profile_id": "coding", "permission_mode": "ask"},
        )

    assert profile_default.status_code == 200
    assert profile_default.json()["permission_mode"] == "agent"
    assert explicit_mode.status_code == 200
    assert explicit_mode.json()["permission_mode"] == "ask"


def test_unknown_professional_agent_profile_is_rejected(tmp_path: Path) -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "agent_profile_id": "missing-profile"},
        )
    assert response.status_code == 400


def test_clear_conversation_messages_resets_context(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path)}).json()
        stamp = now_iso()
        with connect() as db:
            db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)", (conversation["id"], "user", "temporary", stamp))
        cleared = client.delete(f"/api/conversations/{conversation['id']}/messages")
        messages = client.get(f"/api/conversations/{conversation['id']}/messages")
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 1
    assert messages.json() == []


def test_search_provider_status_does_not_expose_secrets(monkeypatch) -> None:
    monkeypatch.setattr("app.api.routes.search.settings.tavily_api_key", "")
    monkeypatch.setattr("app.api.routes.search.settings.brave_api_key", "")
    with TestClient(app) as client:
        response = client.get("/api/search/providers")
    assert response.status_code == 200
    assert response.json()["providers"] == [
        {"name": "tavily", "configured": False, "default": True},
        {"name": "brave", "configured": False, "default": False},
    ]


def test_tauri_origin_is_allowed() -> None:
    with TestClient(app) as client:
        response = client.options("/api/conversations", headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_recoverable_task_can_be_inspected_and_abandoned(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "full"},
        ).json()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, current_phase, resumable, created_at, updated_at, paused_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, conversation["id"], "interrupted", "resume me", "analysis", 1, stamp, stamp, stamp),
            )
        checkpoint = create_checkpoint(task_id, str(tmp_path), "analysis", "process_interrupted", {"goal": "resume me"})

        recoverable = client.get(f"/api/tasks/recoverable?conversation_id={conversation['id']}")
        checkpoints = client.get(f"/api/tasks/{task_id}/checkpoints")
        traces = client.get("/api/tasks/recent")
        abandoned = client.post(f"/api/tasks/{task_id}/abandon")

    assert recoverable.status_code == 200
    assert recoverable.json()[0]["id"] == task_id
    assert recoverable.json()[0]["checkpoints"][0]["sequence"] == checkpoint["sequence"]
    assert checkpoints.status_code == 200
    assert checkpoints.json()[0]["reason"] == "process_interrupted"
    trace = next(item for item in traces.json() if item["id"] == task_id)
    assert trace["checkpoints"][0]["reason"] == "process_interrupted"
    assert trace["operations"] == []
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "cancelled"


def test_runtime_status_unifies_progress_tokens_context_queue_and_checkpoint(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with TestClient(app) as client:
        conversation = client.post(
            "/api/conversations",
            json={"workspace": str(tmp_path), "permission_mode": "full"},
        ).json()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks("
                "id,conversation_id,status,prompt,current_phase,current_step,resumable,"
                "completed_steps,pending_steps,total_tokens,input_tokens,output_tokens,phase_tokens,"
                "model_calls,tool_calls,files_modified,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    conversation["id"],
                    "interrupted",
                    "整理文件",
                    "execution",
                    "write",
                    1,
                    '["read"]',
                    '["write","verify"]',
                    42,
                    30,
                    12,
                    '{"execution":42}',
                    2,
                    1,
                    1,
                    stamp,
                    stamp,
                ),
            )
        enqueue(
            conversation_id=conversation["id"],
            task_id=task_id,
            kind="steer",
            content="完成后运行验证",
            priority="next",
        )
        create_checkpoint(
            task_id,
            str(tmp_path),
            "execution",
            "after_tool_call",
            {"goal": "整理文件", "context_summary": "已完成读取"},
        )
        response = client.get(f"/api/tasks/{task_id}/runtime-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["progress"] == {
        "completed_steps": ["read"],
        "pending_steps": ["write", "verify"],
        "model_calls": 2,
        "tool_calls": 1,
        "files_modified": 1,
    }
    assert payload["tokens"]["total"] == 42
    assert payload["context"]["conversation"]["message_count"] == 0
    assert payload["queue"][0]["content"] == "完成后运行验证"
    assert payload["checkpoint"]["reason"] == "after_tool_call"
    assert payload["checkpoint"]["context_summary"] == "已完成读取"


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


def test_tool_request_cannot_forge_conversation_scope(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        forged_mode = client.post("/api/tools/execute", json={
            "conversation_id": conversation["id"], "workspace": str(tmp_path),
            "permission_mode": "full", "tool": "list_files", "arguments": {"path": "."},
        })
        forged_workspace = client.post("/api/tools/execute", json={
            "conversation_id": conversation["id"], "workspace": str(other),
            "permission_mode": "ask", "tool": "list_files", "arguments": {"path": "."},
        })
    assert forged_mode.status_code == 409
    assert forged_workspace.status_code == 409


def test_tool_request_cannot_attach_another_conversations_task(tmp_path: Path) -> None:
    task_id = uuid.uuid4().hex
    with TestClient(app) as client:
        first = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        second = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, first["id"], "running", "other", now_iso(), now_iso()),
            )
        response = client.post("/api/tools/execute", json={
            "conversation_id": second["id"],
            "workspace": str(tmp_path),
            "permission_mode": "ask",
            "tool": "list_files",
            "arguments": {"path": "."},
            "task_id": task_id,
        })
    assert response.status_code == 409


def test_data_flow_endpoint_returns_metadata_not_payload() -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO data_flow_events(source, sink, classification, fields, redactions, allowed, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("test", "model", "credential", '["content"]', 1, 1, "redacted", now_iso()),
        )
    with TestClient(app) as client:
        response = client.get("/api/security/data-flows?limit=1")
    assert response.status_code == 200
    assert response.json()[0]["fields"] == ["content"]
    assert response.json()[0]["allowed"] is True


def test_legacy_approved_boolean_cannot_bypass_confirmation(tmp_path: Path) -> None:
    with TestClient(app) as client:
        response = client.post("/api/tools/execute", json={
            "workspace": str(tmp_path), "permission_mode": "ask", "tool": "write_file",
            "arguments": {"path": "blocked.txt", "content": "no"}, "approved": True,
        })
    assert response.status_code == 422
    assert not (tmp_path / "blocked.txt").exists()


def test_workspace_memory_uses_same_confirmation_pipeline(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        payload = {
            "conversation_id": conversation["id"], "workspace": str(tmp_path), "permission_mode": "ask",
            "tool": "remember_workspace", "arguments": {"key": "stack", "content": "FastAPI"},
        }
        pending = client.post("/api/tools/execute", json=payload).json()
        assert pending["status"] == "confirmation_required"
        payload["approval_tokens"] = [pending["approval_key"]]
        completed = client.post("/api/tools/execute", json=payload).json()
    assert completed["status"] == "ok"
    assert completed["stored"] is True


def test_memory_crud_and_feedback_endpoints(tmp_path: Path) -> None:
    query = f"?workspace={tmp_path}"
    with TestClient(app) as client:
        created = client.post(
            f"/api/memories{query}",
            json={"key": "test.command", "content": "运行 pytest", "category": "test_command", "tags": ["test"]},
        )
        memory_id = created.json()["id"]
        updated = client.patch(
            f"/api/memories/{memory_id}{query}",
            json={"content": "运行 pytest -q", "confidence": 0.9},
        )
        verified = client.post(f"/api/memories/{memory_id}/feedback{query}", json={"outcome": "verify"})
        listed = client.get(f"/api/memories{query}")
        deleted = client.delete(f"/api/memories/{memory_id}{query}")

    assert created.status_code == 200
    assert updated.json()["content"] == "运行 pytest -q"
    assert verified.json()["last_verified_at"]
    assert listed.json()[0]["tags"] == ["test"]
    assert listed.json()[0]["category"] == "test_command"
    assert deleted.json()["deleted"] is True


def test_global_memory_search_endpoint_works_without_workspace(tmp_path: Path) -> None:
    from app.memory.service import upsert_workspace_memory

    marker = tmp_path.name.replace("-", "")
    stored = upsert_workspace_memory(
        "",
        key=f"global.search.{marker}",
        content=f"全局记忆 {marker} 可以检索",
        namespace="personal",
        source="user",
        verified=True,
    )
    with TestClient(app) as client:
        response = client.get("/api/memories/search", params={"q": marker})

    assert response.status_code == 200
    assert response.json()["counts"]["global"] == 1
    assert response.json()["items"][0]["id"] == stored["id"]
    assert response.json()["items"][0]["search_scope"] == "global"


def test_context_stats_endpoint(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"})
        response = client.get(f"/api/conversations/{created.json()['id']}/context")
    assert response.status_code == 200
    assert response.json()["message_count"] == 0


def test_model_run_trace_endpoint(tmp_path: Path) -> None:
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        response = client.get(f"/api/conversations/{created['id']}/model-runs")
    assert response.status_code == 200
    assert response.json() == []


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
    assert created["title_locked"] is False
    assert renamed.json()["title"] == "新标题"
    assert renamed.json()["title_locked"] is True
    assert deleted.json()["deleted"] is True


def test_active_conversation_cannot_be_deleted_or_change_profile(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "ask"}).json()
        task_id = uuid.uuid4().hex
        with connect() as db:
            db.execute(
                "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (task_id, conversation["id"], "running", "test", now_iso(), now_iso()),
            )
        deleted = client.delete(f"/api/conversations/{conversation['id']}")
        profile = client.patch(f"/api/conversations/{conversation['id']}/profile", json={"agent_profile_id": "general"})
    assert deleted.status_code == 409
    assert "活动或可恢复任务" in deleted.json()["detail"]
    assert profile.status_code == 409
    assert "不能切换" in profile.json()["detail"]


def test_agent_loop_completes_and_records_task(tmp_path: Path, monkeypatch) -> None:
    async def fake_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "完成"}
    monkeypatch.setattr("app.runtime.runner.completion", fake_completion)
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        response = client.post("/api/chat", json={"conversation_id": conversation["id"], "content": "回答我", "task_id": uuid.uuid4().hex})
        tasks = client.get(f"/api/conversations/{conversation['id']}/tasks").json()
    assert response.status_code == 200
    assert response.json()["task_status"] == "completed"
    assert tasks[0]["status"] == "completed"


def test_multi_agent_request_preserves_requested_orchestration(tmp_path: Path, monkeypatch) -> None:
    async def fake_completion(messages, api_key=None, phase="", **kwargs):
        if phase == "multi_agent:planner":
            return {"role": "assistant", "content": "先计划再回答", "_metrics": {"usage": {"total_tokens": 3}}}
        return {"role": "assistant", "content": "完成", "_metrics": {"usage": {"total_tokens": 2}}}

    monkeypatch.setattr("app.runtime.runner.completion", fake_completion)
    task_id = uuid.uuid4().hex
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        response = client.post("/api/chat", json={
            "conversation_id": conversation["id"],
            "content": "回答我",
            "task_id": task_id,
            "orchestration_mode": "planner_executor",
            "agent_count": 1,
        })
        trace = client.get(f"/api/tasks/{task_id}/agents")
        recent = client.get("/api/tasks/recent")

    assert response.status_code == 200
    assert trace.status_code == 200
    assert [item["role"] for item in trace.json()["agents"]] == ["executor", "planner"]
    task = next(item for item in recent.json() if item["id"] == task_id)
    assert task["orchestration_mode"] == "planner_executor"
    assert task["child_agent_count"] == 1
    assert [item["role"] for item in task["agent_runs"]] == ["executor", "planner"]


def test_agent_loop_stops_repeated_tool_calls(tmp_path: Path, monkeypatch) -> None:
    async def repeated_completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}]}
    monkeypatch.setattr("app.runtime.runner.completion", repeated_completion)
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


def test_recent_tasks_include_verification_and_tool_runs(tmp_path: Path) -> None:
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={"workspace": str(tmp_path), "permission_mode": "full"}).json()
        task_id = uuid.uuid4().hex
        now = now_iso()
        with connect() as db:
            db.execute("INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)", (task_id, conversation["id"], "completed", "trace", now, now))
            db.execute("INSERT INTO task_verifications(task_id, status, summary, report, created_at) VALUES(?,?,?,?,?)", (task_id, "passed", "验证通过", '{"status":"passed","checks":[]}', now))
            db.execute("INSERT INTO task_plans(task_id, status, plan, acceptance_criteria, created_at, updated_at) VALUES(?,?,?,?,?,?)", (task_id, "verified", '{"steps":[],"acceptance_criteria":[]}', '[]', now, now))
            db.execute("INSERT INTO task_verification_attempts(task_id, attempt, status, report, evidence_fingerprint, created_at) VALUES(?,?,?,?,?,?)", (task_id, 1, "passed", '{"status":"passed"}', "hash", now))
            db.execute("INSERT INTO task_repair_runs(task_id, attempt, status, retry_scope, before_fingerprint, after_fingerprint, reason, created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, 1, "passed", '["verification_command"]', "before", "after", "fixed", now, now))
        response = client.get("/api/tasks/recent")
    item = next(task for task in response.json() if task["id"] == task_id)
    assert item["verification"]["status"] == "passed"
    assert item["plan"]["steps"] == []
    assert item["verification_attempts"][0]["attempt"] == 1
    assert item["repair_runs"][0]["retry_scope"] == ["verification_command"]
    assert item["tool_runs"] == []
