import asyncio
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from app.database import connect, now_iso
from app.hooks import HookEvent, hook_catalog, register_hook, run_hooks, unregister_hook
from app.lsp import query_lsp
from app.mcp import MCPConnectionManager
from app.sandbox import execute_tool
from app.tool_registry import REGISTRY


def _scope() -> tuple[int, str]:
    stamp = now_iso()
    with connect() as db:
        cursor = db.execute(
            "INSERT INTO conversations(title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?)",
            ("runtime-v4", "", "ask", stamp, stamp),
        )
        conversation_id = int(cursor.lastrowid)
        task_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO agent_tasks(id,conversation_id,status,prompt,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "v4", stamp, stamp),
        )
    return conversation_id, task_id


def test_hook_lifecycle_is_audited_and_failure_isolated() -> None:
    conversation_id, task_id = _scope()

    def healthy(event: HookEvent) -> dict[str, str]:
        return {"tool": str(event.payload["tool"])}

    def broken(_: HookEvent) -> None:
        raise RuntimeError("observer failed")

    register_hook("pre_tool", "healthy", healthy)
    register_hook("pre_tool", "broken", broken)
    try:
        outcomes = asyncio.run(run_hooks(HookEvent("pre_tool", conversation_id, task_id, {"tool": "read_file"})))
    finally:
        unregister_hook("pre_tool", "healthy")
        unregister_hook("pre_tool", "broken")

    assert [item["status"] for item in outcomes] == ["ok", "error"]
    assert hook_catalog()["pre_tool"] == []


def test_hook_payload_is_isolated_and_credentials_are_redacted() -> None:
    conversation_id, task_id = _scope()
    original = {"nested": {"value": "safe"}, "api_key": "sk-secret-value"}
    captured: list[dict] = []

    def observer(event: HookEvent) -> dict[str, str]:
        captured.append(event.payload)
        event.payload["nested"]["value"] = "mutated"
        return {"token": "Bearer private-token"}

    register_hook("pre_tool", "isolated", observer)
    try:
        outcomes = asyncio.run(run_hooks(HookEvent("pre_tool", conversation_id, task_id, original)))
    finally:
        unregister_hook("pre_tool", "isolated")
    assert original["nested"]["value"] == "safe"
    assert "sk-secret-value" not in str(captured)
    assert "private-token" not in str(outcomes)


def test_lsp_query_falls_back_to_workspace_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    monkeypatch.setattr("app.lsp._server_for", lambda _: None)

    result = asyncio.run(query_lsp(str(tmp_path), "sample.py", "definition", symbol="answer"))

    assert result["success"] is True
    assert result["source"] == "workspace-index-fallback"
    assert result["lsp_available"] is False


def test_lsp_query_uses_ordered_json_rpc_handshake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 42\n", encoding="utf-8")
    server = tmp_path / "fake_lsp.py"
    server.write_text(
        """
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line in {b'\\r\\n', b'\\n', b''}:
            break
        key, value = line.decode('ascii').split(':', 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))

def send(value):
    body = json.dumps(value, separators=(',', ':')).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message.get('method') == 'initialize':
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'capabilities': {}}})
    elif message.get('id') == 2:
        send({'jsonrpc': '2.0', 'id': 2, 'result': [{'uri': message['params']['textDocument']['uri'], 'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 5}}}]})
    elif message.get('method') == 'shutdown':
        send({'jsonrpc': '2.0', 'id': message['id'], 'result': None})
    elif message.get('method') == 'exit':
        break
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.lsp._server_for", lambda _: (sys.executable, str(server)))

    result = asyncio.run(query_lsp(str(tmp_path), "sample.py", "definition", line=0, character=1))

    assert result["success"] is True
    assert result["source"] == "language-server"
    assert result["result"][0]["path"] == "sample.py"


def test_lsp_protocol_failure_degrades_to_workspace_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sample.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    server = tmp_path / "broken_lsp.py"
    server.write_text(
        "import sys\nsys.stdout.buffer.write(b'Content-Length: 0\\r\\n\\r\\n')\nsys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.lsp._server_for", lambda _: (sys.executable, str(server)))

    result = asyncio.run(query_lsp(str(tmp_path), "sample.py", "definition", symbol="answer", timeout_seconds=0.5))

    assert result["success"] is True
    assert result["source"] == "workspace-index-fallback"
    assert result["degraded"] is True
    assert result["degradation_reason"] in {"lsp_protocol_error", "lsp_timeout"}


def test_lsp_discards_locations_outside_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "sample.py"
    source.write_text("value = 42\n", encoding="utf-8")
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    server = tmp_path / "outside_lsp.py"
    server.write_text(
        f"""
import json
import sys

def read_message():
    headers = {{}}
    while True:
        line = sys.stdin.buffer.readline()
        if line in {{b'\\r\\n', b'\\n', b''}}:
            break
        key, value = line.decode('ascii').split(':', 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers['content-length'])))

def send(value):
    body = json.dumps(value, separators=(',', ':')).encode()
    sys.stdout.buffer.write(f'Content-Length: {{len(body)}}\\r\\n\\r\\n'.encode() + body)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message.get('method') == 'initialize':
        send({{'jsonrpc': '2.0', 'id': message['id'], 'result': {{'capabilities': {{}}}}}})
    elif message.get('id') == 2:
        send({{'jsonrpc': '2.0', 'id': 2, 'result': [{{'uri': {outside.as_uri()!r}, 'range': {{}}}}]}})
    elif message.get('method') == 'shutdown':
        send({{'jsonrpc': '2.0', 'id': message['id'], 'result': None}})
    elif message.get('method') == 'exit':
        break
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.lsp._server_for", lambda _: (sys.executable, str(server)))

    result = asyncio.run(query_lsp(str(tmp_path), "sample.py", "definition"))

    assert result["success"] is True
    assert result["source"] == "language-server"
    assert result["result"] == []


def test_mcp_connection_manager_reuses_and_invalidates_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def discover(servers, allow_local, *, cache_key):
        nonlocal calls
        calls += 1
        return ([{"type": "function", "function": {"name": "cached"}}], {"cached": (servers[0], "tool")})

    monkeypatch.setattr("app.mcp._discover_mcp_tools_uncached", discover)
    manager = MCPConnectionManager(ttl_seconds=60)
    servers = [{"id": 1, "name": "test", "transport": "http", "url": "https://example.com/mcp"}]

    asyncio.run(manager.discover(servers, False))
    asyncio.run(manager.discover(servers, False))
    assert calls == 1
    assert manager.status()["active_session_sets"] == 1
    manager.invalidate()
    asyncio.run(manager.discover(servers, False))
    assert calls == 2


def test_mcp_connection_manager_rebuilds_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def discover(servers, allow_local, *, cache_key):
        nonlocal calls
        calls += 1
        return ([{"name": f"tool-{calls}"}], {cache_key: (servers[0], "tool")})

    monkeypatch.setattr("app.mcp._discover_mcp_tools_uncached", discover)
    manager = MCPConnectionManager(ttl_seconds=0.01)
    servers = [{"id": 1, "name": "test", "transport": "http", "url": "https://example.com/mcp"}]
    asyncio.run(manager.discover(servers, False))
    time.sleep(0.02)
    definitions, _ = asyncio.run(manager.discover(servers, False))
    assert calls == 2
    assert definitions == [{"name": "tool-2"}]


def test_managed_git_worktree_lifecycle(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)

    created = execute_tool(str(tmp_path), "full", "create_worktree", {"name": "isolated"})
    listed = execute_tool(str(tmp_path), "full", "list_worktrees", {})
    removed = execute_tool(str(tmp_path), "full", "remove_worktree", {"name": "isolated", "force": False}, approval_tokens=[])

    assert created["success"] is True
    assert (tmp_path / ".agent" / "worktrees" / "isolated").is_dir()
    assert any(item["path"] == ".agent/worktrees/isolated" for item in listed["data"]["worktrees"])
    assert removed["success"] is False and removed["status"] == "confirmation_required"
    approval = removed["approval_key"]
    removed = execute_tool(str(tmp_path), "full", "remove_worktree", {"name": "isolated", "force": False}, approval_tokens=[approval])
    assert removed["success"] is True
    assert not (tmp_path / ".agent" / "worktrees" / "isolated").exists()
    assert REGISTRY["remove_worktree"].risk == "critical"


def test_worktree_name_cannot_escape_managed_directory(tmp_path: Path) -> None:
    result = execute_tool(str(tmp_path), "full", "create_worktree", {"name": "../outside"})
    assert result["success"] is False
    assert not (tmp_path.parent / "outside").exists()


def test_dirty_worktree_requires_force_and_critical_confirmation(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, capture_output=True, check=True)
    created = execute_tool(str(tmp_path), "full", "create_worktree", {"name": "dirty"})
    target = tmp_path / created["data"]["path"]
    (target / "dirty.txt").write_text("dirty", encoding="utf-8")
    request = execute_tool(str(tmp_path), "full", "remove_worktree", {"name": "dirty", "force": False})
    assert request["status"] == "confirmation_required"
    with pytest.raises(RuntimeError, match="force"):
        execute_tool(
            str(tmp_path), "full", "remove_worktree", {"name": "dirty", "force": False}, approval_tokens=[request["approval_key"]]
        )
    assert target.exists()
    forced_request = execute_tool(str(tmp_path), "full", "remove_worktree", {"name": "dirty", "force": True})
    assert forced_request["status"] == "confirmation_required"
    removed = execute_tool(
        str(tmp_path), "full", "remove_worktree", {"name": "dirty", "force": True}, approval_tokens=[forced_request["approval_key"]]
    )
    assert removed["success"] is True
    assert not target.exists()
