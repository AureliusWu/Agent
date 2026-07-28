import json
import os
from dataclasses import asdict

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import __version__
from app.config import settings
from app.database import audit, backup_database, database_backups, database_status, restore_database, rows
from app.deployment import validate_deployment_security
from app.diagnostics import create_diagnostic_bundle
from app.diagnostics import diagnostic_manifest
from app.build_info import sidecar_build_info
from app.desktop_lifecycle import request_shutdown
from app.environment import detect_build_environment
from app.kernel.services import kernel_manifest
from app.providers.model_routing import routing_policy
from app.providers.registry import get_provider, provider_health, provider_profile
from app.providers.configuration import (
    ProviderConfiguration,
    load_provider_configuration,
    save_provider_configuration,
)
from app.providers.capabilities import configured_provider_matrix
from app.permissions import (
    PERMISSION_NAMES,
    list_permission_policies,
    revoke_permission_policy,
    set_permission_policy,
)
from app.security.policy import public_security_policy, set_security_domain

router = APIRouter(prefix="/api", tags=["system"])


class ProviderConfigurationInput(BaseModel):
    provider_id: str
    base_url: str = ""
    model: str = ""
    timeout_seconds: int = Field(default=90, ge=1, le=600)
    max_tokens: int = Field(default=8192, ge=1, le=1_000_000)
    max_retries: int = Field(default=2, ge=0, le=5)
    allow_tools: bool = True
    allow_streaming: bool = True


class PermissionPolicyInput(BaseModel):
    permission: str
    effect: str
    scope: str
    workspace: str = ""
    tool: str = "*"
    source: str = "*"
    principal: str = "*"
    administrator_confirmed: bool = False


class SecurityDomainInput(BaseModel):
    domain: str
    administrator_confirmed: bool = False


@router.get("/health")
def health() -> dict:
    db = database_status()
    provider = provider_profile()
    return {
        "status": "ok" if db["status"] == "ok" else "error",
        "version": __version__,
        "build": sidecar_build_info(),
        "database": db,
        "model": provider["default_model"],
        "provider": provider["id"],
        "deployment": validate_deployment_security(),
        "kernel": kernel_manifest(),
    }


@router.get("/diagnostics/status")
def diagnostics_status() -> dict:
    return diagnostic_manifest()


@router.get("/desktop/status")
def desktop_status() -> dict:
    db = database_status()
    provider = provider_profile()
    return {
        "status": "ok" if db["status"] == "ok" else "error",
        "version": __version__,
        "pid": os.getpid(),
        "database": db,
        "model": provider["default_model"],
        "provider": provider["id"],
    }


@router.post("/desktop/shutdown", status_code=202)
def desktop_shutdown() -> dict:
    if not request_shutdown():
        raise HTTPException(409, "当前进程不受桌面宿主管理")
    audit(None, "desktop_shutdown", "sidecar", "accepted")
    return {"status": "shutting_down"}


@router.get("/provider/health")
async def model_health(
    x_model_api_key: str | None = Header(default=None),
    provider_id: str | None = None,
) -> dict:
    try:
        return await provider_health(x_model_api_key, provider_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/provider/configuration")
def model_configuration() -> dict:
    return asdict(load_provider_configuration())


@router.put("/provider/configuration")
def update_model_configuration(payload: ProviderConfigurationInput) -> dict:
    try:
        configured = save_provider_configuration(ProviderConfiguration(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "provider_configuration_updated", configured.provider_id, "ok")
    return asdict(configured)


@router.get("/provider/policy")
def model_policy(provider_id: str | None = None) -> dict:
    try:
        provider = provider_profile(provider_id)
        provider = {**provider, "capabilities": get_provider(provider_id).capabilities()}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        **routing_policy(),
        "provider": provider,
        "capability_matrix": configured_provider_matrix(),
        "budgets": {
            "task_tokens": settings.max_task_tokens,
            "phase_tokens": settings.max_phase_tokens,
            "model_call_tokens": settings.max_model_call_tokens,
            "tool_result_chars": settings.max_tool_result_chars,
            "file_snippet_chars": settings.max_file_snippet_chars,
            "skill_chars": settings.max_skill_context_chars,
            "memory_items": settings.max_memory_items,
        },
        "cache": {
            "read_tool_ttl_seconds": settings.read_cache_ttl_seconds,
            "skill_content": True,
            "project_signature": True,
            "build_environment": True,
            "tool_definitions": True,
        },
        "multi_agent": {
            "enabled": settings.multi_agent_enabled,
            "max_children": settings.multi_agent_max_children,
            "max_concurrency": settings.multi_agent_max_concurrency,
            "total_token_budget": settings.multi_agent_total_token_budget,
            "child_token_budget": settings.multi_agent_child_token_budget,
            "child_timeout_seconds": settings.multi_agent_child_timeout_seconds,
        },
    }


@router.get("/workspaces/environment")
def workspace_environment(workspace: str) -> dict:
    try:
        return detect_build_environment(workspace)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/audit")
def audit_logs(limit: int = 100) -> list[dict]:
    return rows("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))


@router.get("/security/data-flows")
def data_flow_logs(limit: int = 100, task_id: str | None = None) -> list[dict]:
    bounded = min(max(limit, 1), 500)
    events = (
        rows("SELECT * FROM data_flow_events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, bounded))
        if task_id
        else rows("SELECT * FROM data_flow_events ORDER BY id DESC LIMIT ?", (bounded,))
    )
    for event in events:
        try:
            event["fields"] = json.loads(event.get("fields") or "[]")
        except ValueError:
            event["fields"] = []
        event["allowed"] = bool(event.get("allowed"))
    return events


@router.get("/security/policy")
def security_policy() -> dict:
    return {
        **public_security_policy(),
        "permissions": sorted(PERMISSION_NAMES),
        "grants": list_permission_policies(),
    }


@router.put("/security/domain")
def update_security_domain(payload: SecurityDomainInput) -> dict:
    if not payload.administrator_confirmed:
        raise HTTPException(409, "切换开发者或管理员域需要显式管理员确认")
    try:
        domain = set_security_domain(payload.domain)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "security_domain", domain, "ok")
    return public_security_policy()


@router.post("/security/permissions")
def create_permission_policy(payload: PermissionPolicyInput) -> dict:
    if not payload.administrator_confirmed:
        raise HTTPException(409, "持久权限变更需要显式管理员确认")
    try:
        policy = set_permission_policy(
            permission=payload.permission,
            effect=payload.effect,
            scope=payload.scope,
            workspace=payload.workspace,
            tool=payload.tool,
            source=payload.source,
            principal=payload.principal,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(None, "permission_policy_created", str(policy["id"]), "ok", policy)
    return policy


@router.delete("/security/permissions/{policy_id}")
def delete_permission_policy(policy_id: int, administrator_confirmed: bool = False) -> dict:
    if not administrator_confirmed:
        raise HTTPException(409, "撤销持久权限需要显式管理员确认")
    if not revoke_permission_policy(policy_id):
        raise HTTPException(404, "权限策略不存在或已撤销")
    audit(None, "permission_policy_revoked", str(policy_id), "ok")
    return {"id": policy_id, "revoked": True}


@router.get("/tasks/recent")
def recent_tasks(limit: int = 30) -> list[dict]:
    tasks = rows("SELECT * FROM agent_tasks ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 100),))
    for task in tasks:
        for key in ("phase_tokens", "model_route", "agent_profile_snapshot"):
            try:
                task[key] = json.loads(task.get(key) or "{}")
            except ValueError:
                task[key] = {}
        verification = rows("SELECT * FROM task_verifications WHERE task_id=?", (task["id"],))
        task["verification"] = json.loads(verification[0]["report"]) if verification else None
        plans = rows("SELECT status, plan, acceptance_criteria FROM task_plans WHERE task_id=?", (task["id"],))
        task["plan"] = json.loads(plans[0]["plan"]) if plans else None
        task["verification_attempts"] = rows("SELECT attempt, status, report, created_at FROM task_verification_attempts WHERE task_id=? ORDER BY attempt", (task["id"],))
        for attempt in task["verification_attempts"]:
            attempt["report"] = json.loads(attempt["report"])
        task["repair_runs"] = rows("SELECT attempt, status, retry_scope, reason, created_at, finished_at FROM task_repair_runs WHERE task_id=? ORDER BY attempt", (task["id"],))
        for repair in task["repair_runs"]:
            repair["retry_scope"] = json.loads(repair["retry_scope"] or "[]")
        task["checkpoints"] = rows(
            "SELECT sequence, phase, reason, workspace_hash, git_status, created_at FROM task_checkpoints WHERE task_id=? ORDER BY sequence DESC LIMIT 50",
            (task["id"],),
        )
        task["execution_segments"] = rows(
            "SELECT id,sequence,status,reason,phase,context_summary,input_tokens,output_tokens,total_tokens,model_calls,tool_calls,started_at,finished_at "
            "FROM execution_segments WHERE task_id=? ORDER BY sequence",
            (task["id"],),
        )
        task["workspace_instructions"] = rows(
            "SELECT source_path,scope_path,priority,content_hash,content_chars,override,findings,created_at "
            "FROM workspace_instruction_snapshots WHERE task_id=? ORDER BY priority,created_at",
            (task["id"],),
        )
        for instruction in task["workspace_instructions"]:
            instruction["override"] = bool(instruction.get("override"))
            instruction["findings"] = json.loads(instruction.get("findings") or "[]")
        task["operations"] = rows(
            "SELECT execution_id, checkpoint_sequence, tool_call_id, tool, status, side_effect, result, started_at, finished_at FROM task_operations WHERE task_id=? ORDER BY started_at DESC LIMIT 100",
            (task["id"],),
        )
        for operation in task["operations"]:
            operation["result"] = json.loads(operation["result"]) if operation["result"] else None
        runs = rows("SELECT id, source, risk, confirmed, tool, status, input, output, started_at, finished_at, duration_ms FROM tool_runs WHERE task_id=? ORDER BY id", (task["id"],))
        for run in runs:
            for key in ("input", "output"):
                try:
                    run[key] = json.loads(run[key] or "{}")
                except ValueError:
                    run[key] = {"raw": run[key]}
        task["tool_runs"] = runs
        task["skill_runs"] = rows(
            "SELECT name,path,version,source,content_chars,content_tokens,trigger_reason,dependency_chain,status,error,created_at "
            "FROM skill_runs WHERE task_id=? ORDER BY id",
            (task["id"],),
        )
        for skill_run in task["skill_runs"]:
            try:
                skill_run["dependency_chain"] = json.loads(skill_run.get("dependency_chain") or "[]")
            except ValueError:
                skill_run["dependency_chain"] = []
        task["data_flows"] = rows(
            "SELECT source, sink, classification, fields, redactions, allowed, reason, created_at FROM data_flow_events WHERE task_id=? ORDER BY id DESC LIMIT 100",
            (task["id"],),
        )
        for flow in task["data_flows"]:
            try:
                flow["fields"] = json.loads(flow.get("fields") or "[]")
            except ValueError:
                flow["fields"] = []
            flow["allowed"] = bool(flow.get("allowed"))
        task["security_snapshots"] = rows(
            "SELECT id, reason, status, file_count, total_bytes, created_at, restored_at FROM security_snapshots WHERE task_id=? ORDER BY created_at DESC LIMIT 20",
            (task["id"],),
        )
        multi_agent_task = str(task.get("orchestration_mode") or "single") != "single"
        task["agent_runs"] = (
            rows(
                "SELECT id, parent_agent_id, role, orchestration_mode, status, token_budget, tokens_used, tool_allowlist, file_scope, risk_level, depth, error, started_at, finished_at "
                "FROM agent_runs WHERE parent_task_id=? ORDER BY depth, started_at",
                (task["id"],),
            )
            if multi_agent_task
            else []
        )
        for agent in task["agent_runs"]:
            for key in ("tool_allowlist", "file_scope"):
                try:
                    agent[key] = json.loads(agent.get(key) or "[]")
                except ValueError:
                    agent[key] = []
        task["file_locks"] = (
            rows(
                "SELECT path, holder_agent_id, status, version_before, version_after, acquired_at, released_at FROM agent_file_locks WHERE holder_task_id=? ORDER BY acquired_at DESC LIMIT 100",
                (task["id"],),
            )
            if multi_agent_task
            else []
        )
        task["model_runs"] = rows(
            "SELECT provider, model, phase, route_tier, task_type, route_confidence, input_tokens, output_tokens, total_tokens, "
            "cached_input_tokens, uncached_input_tokens, cache_write_tokens, estimated_cost_usd, duration_ms, success, "
            "first_token_ms, error_type, retry_count, started_at FROM model_runs WHERE task_id=? ORDER BY id",
            (task["id"],),
        )
        phase_costs: dict[str, dict[str, float | int]] = {}
        for run in task["model_runs"]:
            phase = str(run.get("phase") or "analysis")
            summary = phase_costs.setdefault(phase, {"calls": 0, "tokens": 0, "duration_ms": 0, "estimated_cost_usd": 0.0})
            summary["calls"] = int(summary["calls"]) + 1
            summary["tokens"] = int(summary["tokens"]) + int(run.get("total_tokens") or 0)
            summary["duration_ms"] = int(summary["duration_ms"]) + int(run.get("duration_ms") or 0)
            summary["estimated_cost_usd"] = round(float(summary["estimated_cost_usd"]) + float(run.get("estimated_cost_usd") or 0), 8)
        task["phase_costs"] = phase_costs
        working = rows("SELECT state, updated_at FROM task_working_memory WHERE task_id=?", (task["id"],))
        task["working_memory"] = json.loads(working[0]["state"]) if working else None
    return tasks


@router.get("/usage/summary")
def usage_summary(days: int = 30) -> dict:
    bounded_days = min(max(days, 1), 365)
    totals = rows(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens, "
        "COALESCE(SUM(uncached_input_tokens),0) AS uncached_input_tokens, "
        "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd, "
        "COALESCE(AVG(duration_ms),0) AS average_duration_ms FROM model_runs"
    )[0]
    cache_denominator = int(totals["cached_input_tokens"]) + int(totals["uncached_input_tokens"])
    totals["cache_hit_rate"] = (
        round(int(totals["cached_input_tokens"]) / cache_denominator, 4)
        if cache_denominator
        else 0.0
    )
    models = rows(
        "SELECT provider, model, COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens, "
        "COALESCE(SUM(uncached_input_tokens),0) AS uncached_input_tokens, "
        "COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd FROM model_runs GROUP BY provider, model ORDER BY total_tokens DESC"
    )
    daily = rows(
        "SELECT substr(started_at,1,10) AS date, COUNT(*) AS requests, COALESCE(SUM(input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens, "
        "COALESCE(SUM(uncached_input_tokens),0) AS uncached_input_tokens, COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(estimated_cost_usd),0) AS estimated_cost_usd FROM model_runs "
        "WHERE datetime(started_at) >= datetime('now', ?) GROUP BY substr(started_at,1,10) ORDER BY date",
        (f"-{bounded_days} days",),
    )
    return {"period_days": bounded_days, "totals": totals, "models": models, "daily": daily}


@router.get("/database/backups")
def list_database_backups() -> list[dict]:
    return database_backups()


@router.post("/database/backups")
def create_database_backup() -> dict:
    result = backup_database()
    audit(None, "database_backup", result["name"], "ok")
    return result


@router.post("/database/restore/{name}")
def restore_database_backup(name: str) -> dict:
    try:
        result = restore_database(name)
        audit(None, "database_restore", name, "ok")
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/diagnostics/export")
def export_diagnostics() -> FileResponse:
    result = create_diagnostic_bundle()
    audit(None, "diagnostics_export", result["name"], "ok", {"size": result["size"]})
    return FileResponse(result["path"], media_type="application/zip", filename=result["name"])
