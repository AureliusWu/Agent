from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.personality.affect import current_relationship
from app.config import settings
from app.database import audit, connect, now_iso, rows
from app.personality.identity_service import AGENT_ID, active_identity
from app.memory.catalog import authoritative_user_memories
from app.memory.long_term import list_memories
from app.runtime.task_state import NONTERMINAL_TASK_STATUSES, task_status_values


def _autobiography_path() -> Path:
    path = Path(settings.database_path).resolve().parent / "kokoro_autobiography.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _memory_lines(items: list[dict[str, Any]], *, limit: int) -> list[str]:
    return [
        f"- [{item['id']}] {item['content']}" if not item.get("title")
        else f"- [{item['id']}] {item['title']}：{item['content']}"
        for item in items[:limit]
    ]


def build_continuity_snapshot() -> dict[str, Any]:
    identity = active_identity()["identity"]
    relationship = current_relationship()
    active = list_memories(status="active", include_sensitive=False)
    semantic = [item for item in active if item["memory_type"] == "semantic"]
    episodic = [item for item in active if item["memory_type"] == "episodic"]
    procedural = [item for item in active if item["memory_type"] == "procedural"]
    pending_statuses = task_status_values(NONTERMINAL_TASK_STATUSES)
    pending_tasks = rows(
        "SELECT id,prompt,status,current_step,updated_at FROM agent_tasks "
        f"WHERE status IN ({','.join('?' for _ in pending_statuses)}) "
        "ORDER BY updated_at DESC LIMIT 20",
        pending_statuses,
    )
    memory_ids = [item["id"] for item in [*semantic[:10], *episodic[:10], *procedural[:8]]]
    task_ids = [item["id"] for item in pending_tasks]
    relationship_state = relationship["state"]
    summary_sections = [
        "# 我是谁",
        f"{identity['self_description']}（身份版本 {identity['version']}）",
        "",
        "# 我的核心原则",
        *[f"- {value}" for value in identity.get("core_principles", [])],
        "",
        "# 我与管理员的关系",
        (
            f"关系状态来自本地数据库：信任 {relationship_state['trust']:.2f}，熟悉 {relationship_state['familiarity']:.2f}，"
            f"协作深度 {relationship_state['collaboration_depth']:.2f}；共同记录 {relationship['shared_history_count']} 次。"
        ),
        "",
        "# 我们共同经历的重要事件",
        *(_memory_lines(episodic, limit=10) or ["- 暂无有来源的情景记忆"]),
        "",
        "# 当前事实与项目",
        *(_memory_lines(semantic, limit=10) or ["- 暂无有来源的语义记忆"]),
        "",
        "# 稳定协作方式",
        *(_memory_lines(procedural, limit=8) or ["- 暂无有来源的程序记忆"]),
        "",
        "# 未完成事项",
        *([f"- [{item['id']}] {item['prompt']}（{item['status']} / {item.get('current_step') or 'unknown'}）" for item in pending_tasks] or ["- 暂无未完成任务"]),
        "",
        "# 来源说明",
        "本文件由本地数据库确定性生成，不使用文学补全。方括号内为可追溯的记忆或任务 ID。",
    ]
    summary = "\n".join(summary_sections).strip() + "\n"
    snapshot_id = f"continuity_{uuid.uuid4().hex}"
    with connect() as db:
        db.execute(
            "INSERT INTO continuity_snapshots(id,agent_id,summary,source_memory_ids_json,source_task_ids_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (snapshot_id, AGENT_ID, summary, json.dumps(memory_ids), json.dumps(task_ids), now_iso()),
        )
    _autobiography_path().write_text(summary, encoding="utf-8")
    return {"id": snapshot_id, "summary": summary, "memory_ids": memory_ids, "task_ids": task_ids, "path": str(_autobiography_path())}


def consolidate_memories(*, trigger_type: str = "manual") -> dict[str, Any]:
    if trigger_type not in {"manual", "idle", "conversation"}:
        raise ValueError("Unsupported consolidation trigger")
    run_id = f"consolidation_{uuid.uuid4().hex}"
    started = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO consolidation_runs(id,agent_id,trigger_type,status,result_json,created_at) VALUES(?,?,?,?,?,?)",
            (run_id, AGENT_ID, trigger_type, "running", "{}", started),
        )
    archived: list[str] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with connect() as db:
        candidates = db.execute(
            "SELECT id FROM memories WHERE agent_id=? AND status='active' AND source_type='agent_inference' "
            "AND user_confirmed=0 AND is_locked=0 AND importance<0.25 AND updated_at<?",
            (AGENT_ID, cutoff),
        ).fetchall()
        archived = [str(item[0]) for item in candidates]
        if archived:
            placeholders = ",".join("?" for _ in archived)
            db.execute(f"UPDATE memories SET status='archived',updated_at=? WHERE id IN ({placeholders})", (now_iso(), *archived))
    snapshot = build_continuity_snapshot()
    result = {
        "run_id": run_id,
        "archived_memory_ids": archived,
        "continuity_snapshot_id": snapshot["id"],
        "source_memory_count": len(snapshot["memory_ids"]),
        "pending_task_count": len(snapshot["task_ids"]),
    }
    with connect() as db:
        db.execute(
            "UPDATE consolidation_runs SET status='completed',result_json=?,finished_at=? WHERE id=?",
            (json.dumps(result), now_iso(), run_id),
        )
    audit(None, "memory_consolidation", run_id, "ok", result)
    return result


def latest_continuity() -> dict[str, Any]:
    items = rows(
        "SELECT id,summary,source_memory_ids_json,source_task_ids_json,created_at FROM continuity_snapshots "
        "WHERE agent_id=? ORDER BY created_at DESC LIMIT 1",
        (AGENT_ID,),
    )
    if not items:
        return build_continuity_snapshot()
    item = items[0]
    item["memory_ids"] = json.loads(item.pop("source_memory_ids_json"))
    item["task_ids"] = json.loads(item.pop("source_task_ids_json"))
    item["path"] = str(_autobiography_path())
    return item


def maybe_consolidate_idle() -> dict[str, Any] | None:
    total = len(authoritative_user_memories())
    if int(total) < 25:
        return None
    latest = rows("SELECT created_at FROM consolidation_runs WHERE agent_id=? ORDER BY created_at DESC LIMIT 1", (AGENT_ID,))
    if latest:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(latest[0]["created_at"]) < timedelta(hours=6):
                return None
        except ValueError:
            pass
    return consolidate_memories(trigger_type="idle")
