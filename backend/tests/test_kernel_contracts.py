import asyncio
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.database import connect, init_db, now_iso
from app.extension_sdk import ExtensionManifest
from app.kernel.adapters import SqliteTaskStore
from app.kernel.contracts import ContextProvider, Evaluator, ExtensionProvider, MemoryProvider, ModelProvider, PermissionPolicy, TaskStore, ToolProvider, TraceExporter, Verifier, WorkspaceProvider
from app.kernel.errors import KernelContractError, KernelError
from app.kernel.services import SERVICE_CONTRACTS, KernelServices, build_kernel_services, kernel_manifest, validate_kernel_services
from app.task_state import TaskStatus


def test_default_composition_satisfies_every_stable_contract() -> None:
    async def completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": messages[-1]["content"], "api_key_seen": api_key, "phase": kwargs.get("phase")}

    services = build_kernel_services(completion)
    contracts = {
        "model": ModelProvider,
        "tools": ToolProvider,
        "permissions": PermissionPolicy,
        "context": ContextProvider,
        "memory": MemoryProvider,
        "workspace": WorkspaceProvider,
        "tasks": TaskStore,
        "evaluator": Evaluator,
        "verifier": Verifier,
        "trace": TraceExporter,
        "extensions": ExtensionProvider,
    }

    assert contracts == SERVICE_CONTRACTS
    assert all(isinstance(getattr(services, name), contract) for name, contract in contracts.items())
    result = asyncio.run(services.model.complete([{"role": "user", "content": "contract"}], "temporary", phase="test"))
    assert result == {"role": "assistant", "content": "contract", "api_key_seen": "temporary", "phase": "test"}

    manifest = kernel_manifest(services)
    assert manifest["contract_version"] == "1.0"
    assert manifest["composition"] == "trusted_internal"
    assert manifest["extension_replaceable"] is False
    assert set(manifest["services"]) == set(contracts)


def test_tool_adapter_is_replaceable_only_through_trusted_composition() -> None:
    calls: list[dict] = []

    async def completion(messages, api_key=None, **kwargs):
        return {"role": "assistant", "content": "ok"}

    async def tool_execute(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "source": "contract-test"}

    services = build_kernel_services(completion, tool_execute)
    result = asyncio.run(services.tools.execute(name="read_file", workspace="C:/workspace"))

    assert result == {"status": "ok", "source": "contract-test"}
    assert calls[0]["name"] == "read_file"
    assert calls[0]["workspace"] == "C:/workspace"
    assert callable(calls[0]["permission_fn"])


def test_invalid_service_composition_is_rejected() -> None:
    services = build_kernel_services()
    invalid = replace(services, tools=object())

    with pytest.raises(KernelContractError) as caught:
        validate_kernel_services(invalid)  # type: ignore[arg-type]

    assert caught.value.code == "contract_violation"
    assert caught.value.details == {"invalid_services": ["tools"]}
    assert caught.value.as_dict()["component"] == "contracts"


def test_task_store_cannot_write_completed_but_verifier_can(tmp_path: Path) -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "kernel contract", str(tmp_path), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "verify", stamp, stamp),
        )

    store = SqliteTaskStore()
    store.update_task(task_id, TaskStatus.PAUSED, current_step="contract")
    with pytest.raises(KernelContractError, match="Verifier"):
        store.update_task(task_id, TaskStatus.COMPLETED)
    assert store.task(task_id)["status"] == "paused"  # type: ignore[index]

    services = build_kernel_services()
    final = services.verifier.finalize(task_id, {"status": "passed", "reason": "contract evidence"}, current_step="completed")
    assert final == TaskStatus.COMPLETED
    assert store.task(task_id)["status"] == "completed"  # type: ignore[index]


def test_context_workspace_permission_trace_and_error_contracts(tmp_path: Path) -> None:
    init_db()
    services = build_kernel_services()
    current = services.context.current(
        user_task="inspect",
        phase="analysis",
        step="start",
        recent_tool_results=[],
        errors=[],
        modified_files=[],
        pending_confirmations=[],
    )
    working = services.context.working(
        goal="inspect",
        completed_steps=[],
        pending_steps=["read"],
        plan=["read"],
        failed_approaches=[],
        constraints=[],
        dependencies=[],
        risks=[],
        verification={},
    )
    assert "当前上下文" in services.context.render(current, working)
    assert services.workspace.root(str(tmp_path)) == tmp_path.resolve()
    assert services.permissions.authorize(mode="full", risk="low", tool="read_file", arguments={"path": "README.md"}, workspace=str(tmp_path)).allowed is True

    target = f"kernel-{uuid.uuid4().hex}"
    services.trace.audit(None, "kernel_contract", target, "ok", {"api_key": "sk-contract-secret"})
    with connect() as db:
        details = db.execute("SELECT details FROM audit_logs WHERE target=? ORDER BY id DESC LIMIT 1", (target,)).fetchone()[0]
    assert "sk-contract-secret" not in details

    error = KernelError("temporary", "adapter_timeout", component="model", retryable=True, details={"attempt": 2})
    assert error.as_dict() == {
        "code": "adapter_timeout",
        "component": "model",
        "message": "temporary",
        "retryable": True,
        "details": {"attempt": 2},
    }


def test_extension_manifest_cannot_replace_kernel_services() -> None:
    payload = {
        "schema_version": 1,
        "id": "contract.example",
        "name": "Contract Example",
        "version": "1.0.0",
        "author": "tests",
        "min_app_version": "0.13.0",
        "providers": {"model": "arbitrary.module:Provider"},
    }

    with pytest.raises(ValidationError, match="providers"):
        ExtensionManifest.model_validate(payload)


def test_task_store_records_tool_trace_through_stable_adapter(tmp_path: Path) -> None:
    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    task_id = uuid.uuid4().hex
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id, title, workspace, permission_mode, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "trace", str(tmp_path), "full", stamp, stamp),
        )
        db.execute(
            "INSERT INTO agent_tasks(id, conversation_id, status, prompt, created_at, updated_at) VALUES(?,?,?,?,?,?)",
            (task_id, conversation_id, "running", "trace", stamp, stamp),
        )

    store = SqliteTaskStore()
    store.record_tool_run(
        conversation_id=conversation_id,
        task_id=task_id,
        tool="read_file",
        arguments={"path": "README.md"},
        result={"status": "ok", "success": True},
        started=stamp,
        started_perf=time.perf_counter(),
        risk="low",
        confirmed=False,
        execution_id=uuid.uuid4().hex,
    )
    with connect() as db:
        row = db.execute("SELECT tool, status, source FROM tool_runs WHERE task_id=?", (task_id,)).fetchone()
    assert tuple(row) == ("read_file", "ok", "builtin")
