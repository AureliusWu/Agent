import uuid

from fastapi.testclient import TestClient

from app.admin_action_grants import consume_admin_action_grant, issue_admin_action_grant
from app.main import app
from app.memory.long_term import create_memory, delete_memory, update_memory
from app.memory.search import MemorySearchQuery, memory_search_service
from app.memory.service import upsert_workspace_memory
from app.security.trust import REDACTED


def marker(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex}"


def authorization(operation: str, target_id: str, payload: dict):
    ui_session_id = f"test-{uuid.uuid4().hex}"
    grant = issue_admin_action_grant(
        operation=operation, target_id=target_id, payload=payload, ui_session_id=ui_session_id
    )
    return consume_admin_action_grant(
        grant["grant_token"],
        operation=operation,
        target_id=target_id,
        payload=payload,
        ui_session_id=ui_session_id,
    )


def test_search_finds_titles_content_and_tags_across_four_types() -> None:
    needle = marker("nebula")
    expected: set[str] = set()
    for index, memory_type in enumerate(("semantic", "episodic", "procedural", "relationship")):
        values = {
            "title": f"{needle} title" if index == 0 else f"memory {index}",
            "content": f"body {needle}" if index == 1 else f"body {index}",
            "metadata": {"tags": [needle]} if index >= 2 else {},
        }
        expected.add(create_memory(memory_type=memory_type, source_type="manual_entry", **values)["id"])

    result = memory_search_service.search(MemorySearchQuery(query=needle, limit=10))

    assert {item["id"] for item in result["items"]} == expected
    assert {item["memory_type"] for item in result["items"]} == {"semantic", "episodic", "procedural", "relationship"}
    assert all(item["matched_fields"] for item in result["items"])
    assert all(set(item["ranking"]) == {"lexical", "fts_rank", "importance", "confidence", "recency", "retrieval"} for item in result["items"])
    assert all(item["namespace"] == "personal_long_term" for item in result["items"])


def test_search_filters_status_type_and_paginates() -> None:
    needle = marker("filter")
    semantic = create_memory(memory_type="semantic", content=f"{needle} semantic")
    procedural = create_memory(memory_type="procedural", content=f"{needle} procedure")
    update_memory(
        semantic["id"],
        {"status": "archived"},
        authorization=authorization("memory.update", semantic["id"], {"status": "archived"}),
    )

    active = memory_search_service.search(
        MemorySearchQuery(query=needle, memory_types=("procedural",), statuses=("active",), limit=1)
    )
    archived = memory_search_service.search(
        MemorySearchQuery(query=needle, statuses=("archived",), limit=1)
    )

    assert [item["id"] for item in active["items"]] == [procedural["id"]]
    assert active["page"] == {"offset": 0, "limit": 1, "total": 1, "has_more": False, "next_cursor": None}
    assert [item["id"] for item in archived["items"]] == [semantic["id"]]


def test_sensitive_memory_is_excluded_by_default_and_redacted_after_authorization() -> None:
    needle = marker("private")
    secret = create_memory(
        memory_type="semantic",
        title=f"private {needle}",
        content=f"confidential {needle}",
        is_sensitive=True,
    )

    excluded = memory_search_service.search(MemorySearchQuery(query=needle))
    redacted = memory_search_service.search(MemorySearchQuery(query=needle, sensitive_mode="redacted"))

    assert secret["id"] not in {item["id"] for item in excluded["items"]}
    assert redacted["items"][0]["id"] == secret["id"]
    assert redacted["items"][0]["title"] == "敏感记忆"
    assert needle not in redacted["items"][0]["content"]
    assert redacted["items"][0]["matched_terms"] == []
    assert redacted["items"][0]["matched_fields"] == []
    assert redacted["items"][0]["tags"] == []
    assert redacted["items"][0]["metadata"] == {}


def test_search_index_tracks_update_supersede_and_delete_without_crossing_namespace(tmp_path) -> None:
    old = marker("old-keyword")
    new = marker("new-keyword")
    memory = create_memory(memory_type="procedural", content=f"run {old}")
    upsert_workspace_memory(str(tmp_path), key="project-only", content=f"project {new}", verified=True)
    changes = {"content": f"run {new}"}
    update_memory(memory["id"], changes, authorization=authorization("memory.update", memory["id"], changes))

    assert memory_search_service.search(MemorySearchQuery(query=old))["items"] == []
    updated = memory_search_service.search(MemorySearchQuery(query=new))["items"]
    assert [item["id"] for item in updated] == [memory["id"]]
    delete_memory(memory["id"], authorization=authorization("memory.delete", memory["id"], {}))
    assert memory_search_service.search(MemorySearchQuery(query=new))["items"] == []


def test_superseded_memory_is_removed_from_active_results_but_remains_traceable() -> None:
    needle = marker("supersede")
    old = create_memory(
        memory_type="semantic",
        content=f"{needle} old",
        source_type="user_confirmed",
        user_confirmed=True,
        metadata={"subject": needle, "predicate": "value"},
    )
    new = create_memory(
        memory_type="semantic",
        content=f"{needle} new",
        source_type="user_confirmed",
        user_confirmed=True,
        metadata={"subject": needle, "predicate": "value"},
    )

    active = memory_search_service.search(MemorySearchQuery(query=needle, statuses=("active",)))
    superseded = memory_search_service.search(MemorySearchQuery(query=needle, statuses=("superseded",)))

    assert [item["id"] for item in active["items"]] == [new["id"]]
    assert [item["id"] for item in superseded["items"]] == [old["id"]]


def test_search_supports_provenance_flags_validity_thresholds_and_cursor() -> None:
    needle = marker("advanced-filter")
    first = create_memory(
        memory_type="semantic", content=f"{needle} confirmed record", source_type="user_confirmed", user_confirmed=True,
        is_locked=True, importance=0.9, confidence=0.95, valid_from="2026-01-01", valid_until="2026-12-31",
    )
    create_memory(memory_type="semantic", content=f"{needle} inferred record", source_type="agent_inference", importance=0.2, confidence=0.2)
    request = MemorySearchQuery(
        query=needle, source_types=("user_confirmed",), user_confirmed=True, is_locked=True,
        valid_from="2026-06-01", valid_to="2026-06-30", min_importance=0.8, min_confidence=0.9, limit=1,
    )
    result = memory_search_service.search(request)
    assert [item["id"] for item in result["items"]] == [first["id"]]

    create_memory(memory_type="episodic", content=f"{needle} separate episode", importance=1.0)
    page_one = memory_search_service.search(MemorySearchQuery(query=needle, limit=1))
    page_two = memory_search_service.search(MemorySearchQuery(query=needle, limit=1, cursor=page_one["page"]["next_cursor"]))
    assert page_one["page"]["next_cursor"]
    assert page_one["items"][0]["id"] != page_two["items"][0]["id"]


def test_search_api_is_structured_and_sensitive_access_is_payload_bound() -> None:
    needle = marker("api-search")
    secret = create_memory(memory_type="relationship", content=needle, is_sensitive=True)
    request = {
        "query": needle,
        "memory_types": [],
        "statuses": ["active"],
        "sensitive_mode": "redacted",
        "sort": "relevance",
        "offset": 0,
        "limit": 20,
    }
    ui_session_id = f"test-{uuid.uuid4().hex}"
    grant = issue_admin_action_grant(
        operation="memory.search_sensitive",
        target_id="search",
        payload=request,
        ui_session_id=ui_session_id,
    )

    with TestClient(app) as client:
        unsafe_list = client.get("/api/long-term-memories?include_sensitive=true")
        denied = client.post("/api/long-term-memories/search", json=request)
        allowed = client.post(
            "/api/long-term-memories/search",
            json={**request, "admin_grant_token": grant["grant_token"], "ui_session_id": ui_session_id},
        )

    assert unsafe_list.status_code == 403
    assert denied.status_code == 403
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["page"]["total"] == 1
    assert payload["items"][0]["id"] == secret["id"]
    assert payload["items"][0]["content"] == REDACTED


def test_memory_create_api_binds_grant_to_submitted_fields_without_default_mismatch() -> None:
    values = {
        "memory_type": "semantic",
        "content": marker("ui-create"),
        "source_type": "manual_entry",
        "confidence": 1,
        "importance": 0.8,
        "user_confirmed": True,
        "is_locked": False,
        "is_sensitive": False,
        "metadata": {"tags": ["ui"]},
    }
    ui_session_id = f"test-{uuid.uuid4().hex}"
    grant = issue_admin_action_grant(
        operation="memory.create", target_id="new", payload=values, ui_session_id=ui_session_id,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/long-term-memories",
            json={**values, "admin_grant_token": grant["grant_token"], "ui_session_id": ui_session_id},
        )
    assert response.status_code == 200
    assert response.json()["content"] == values["content"]
