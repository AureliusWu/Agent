from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from typing import Any, Awaitable, Callable

from app.database import connect, now_iso, rows
from app.providers.model_routing import max_output_tokens_for_tier, model_for_tier
from app.providers.registry import completion
from app.security.trust import REDACTED, redact_payload


CompletionCallable = Callable[..., Awaitable[dict[str, Any]]]
_running: set[asyncio.Task[None]] = set()
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s<>:\"|?*]+\\)*[^\s<>:\"|?*]*")
_UNIX_PATH = re.compile(r"(?<![\w.])/(?:[^\s/]+/)+[^\s/]*")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_ADDRESS = re.compile(r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|镇|乡|街道|路|街|号)[\u4e00-\u9fff\d-]{0,24}")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _input_hash(user_message: str, assistant_response: str) -> str:
    encoded = json.dumps([user_message, assistant_response], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_text(value: str) -> str:
    cleaned, _ = redact_payload(value)
    text = str(cleaned)
    for pattern in (_WINDOWS_PATH, _UNIX_PATH, _PHONE, _IDENTITY, _ADDRESS):
        text = pattern.sub(" [PRIVATE] ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_title(value: str) -> str | None:
    title = _sanitize_text(value).strip(" \t\r\n\"'“”‘’`《》<>:：。.!！?？")
    title = re.sub(r"^(?:标题|Title)\s*[:：]\s*", "", title, flags=re.I)
    title = re.sub(r"^关于", "", title)
    title = re.sub(r"(?:的)?(?:对话|聊天)$", "", title)
    title = title.replace(REDACTED, "").replace("[PRIVATE]", "")
    title = re.sub(r"\s+", " ", title).strip(" -—_，,。.")
    if not title or title == "新对话":
        return None
    if _CJK.search(title):
        title = re.sub(r"\s+", "", title)[:16]
        return title if 4 <= len(title) <= 16 else None
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+._-]*", title)
    return " ".join(words[:8]) if 3 <= len(words) <= 8 else None


def fallback_title(user_message: str) -> str:
    text = _sanitize_text(user_message).replace(REDACTED, "").replace("[PRIVATE]", "")
    text = re.sub(r"^(?:请|请你|帮我|帮忙|我想|能否|可以)\s*", "", text, flags=re.I)
    if _CJK.search(text):
        candidate = re.sub(r"[^\u3400-\u9fffA-Za-z0-9+._-]", "", text)[:16]
        if len(candidate) < 4:
            candidate = "任务内容概览"
        return candidate
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+._-]*", text)[:8]
    if len(words) < 3:
        return "New Task Overview"
    return " ".join(words)


def _job(job_id: str) -> dict[str, Any] | None:
    items = rows("SELECT * FROM conversation_title_jobs WHERE id=?", (job_id,))
    return items[0] if items else None


def _budgeted_conversation(conversation_id: int) -> bool:
    # Restart recovery has no originating task ContextVar. Keep orphan title
    # jobs local if the conversation belongs to a dollar-constrained task.
    return bool(rows("SELECT 1 FROM agent_tasks WHERE conversation_id=? AND cost_budget_limit IS NOT NULL LIMIT 1", (conversation_id,)))


def _local_title_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(str(job["input_json"]))
    title = fallback_title(str(payload["user"]))
    stamp = now_iso()
    with connect() as db:
        updated = db.execute(
            "UPDATE conversations SET title=?,title_source='fallback',title_generated_at=?,title_version=title_version+1,"
            "title_input_hash=?,updated_at=? WHERE id=? AND title_locked=0 AND (title_input_hash IS NULL OR title_input_hash!=?)",
            (title, stamp, job["input_hash"], stamp, job["conversation_id"], job["input_hash"]),
        )
        db.execute("UPDATE conversation_title_jobs SET status='completed',last_error=NULL,updated_at=?,finished_at=? WHERE id=?", (stamp, stamp, job["id"]))
    return {**(_job(str(job["id"])) or {}), "title": title if updated.rowcount else None, "title_source": "fallback"}


def ensure_title_job(conversation_id: int, user_message: str, assistant_response: str) -> dict[str, Any] | None:
    safe_user = _sanitize_text(user_message)[:4_000]
    safe_response = _sanitize_text(assistant_response)[:1_000]
    digest = _input_hash(safe_user, safe_response)
    stamp = now_iso()
    with connect() as db:
        conversation = db.execute(
            "SELECT title,title_locked,title_input_hash FROM conversations WHERE id=?",
            (conversation_id,),
        ).fetchone()
        if not conversation or int(conversation["title_locked"] or 0) or str(conversation["title"]).strip() != "新对话":
            return None
        assistant_count = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (conversation_id,),
        ).fetchone()[0]
        if int(assistant_count) != 1:
            return None
        existing = db.execute("SELECT * FROM conversation_title_jobs WHERE conversation_id=?", (conversation_id,)).fetchone()
        if existing:
            return dict(existing)
        job_id = f"title_{uuid.uuid4().hex}"
        db.execute(
            "INSERT INTO conversation_title_jobs(id,conversation_id,status,input_hash,input_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (job_id, conversation_id, "pending", digest, json.dumps({"user": safe_user, "assistant": safe_response}, ensure_ascii=False), stamp, stamp),
        )
    return _job(job_id)


async def run_title_job(
    job_id: str,
    api_key: str | None = None,
    *,
    completion_fn: CompletionCallable | None = None,
) -> dict[str, Any] | None:
    job = _job(job_id)
    if not job or job["status"] == "completed":
        return job
    if _budgeted_conversation(int(job["conversation_id"])):
        return _local_title_job(job)
    stamp = now_iso()
    with connect() as db:
        claimed = db.execute(
            "UPDATE conversation_title_jobs SET status='running',attempts=attempts+1,updated_at=? "
            "WHERE id=? AND status IN ('pending','running','failed')",
            (stamp, job_id),
        )
        if not claimed.rowcount:
            return _job(job_id)
    payload = json.loads(str(job["input_json"]))
    source = "auto"
    error: str | None = None
    try:
        complete = completion_fn or completion
        response = await complete(
            [
                {
                    "role": "system",
                    "content": (
                        "为对话生成一个标题，只输出标题。中文 4-16 个汉字，英文 3-8 个单词。"
                        "不要使用引号、关于某某的对话、密钥、私人路径、身份信息、推理或工具日志。"
                    ),
                },
                {"role": "user", "content": f"用户首条消息：{payload['user']}\n首条回答摘要：{payload['assistant']}"},
            ],
            api_key,
            tools=[],
            model=model_for_tier("light"),
            max_tokens=min(80, max_output_tokens_for_tier("light")),
            phase="title",
            route_tier="light",
            task_type="title",
            route_confidence=1.0,
            conversation_id=int(job["conversation_id"]),
            task_id=None,
        )
        title = _normalize_title(str(response.get("content") or ""))
        if title is None:
            source = "fallback"
            title = fallback_title(str(payload["user"]))
    except asyncio.CancelledError:
        with connect() as db:
            db.execute("UPDATE conversation_title_jobs SET status='pending',updated_at=? WHERE id=?", (now_iso(), job_id))
        raise
    except Exception as exc:
        source = "fallback"
        error = type(exc).__name__
        title = fallback_title(str(payload["user"]))
    finished = now_iso()
    with connect() as db:
        updated = db.execute(
            "UPDATE conversations SET title=?,title_source=?,title_generated_at=?,title_version=title_version+1,"
            "title_input_hash=?,updated_at=? WHERE id=? AND title_locked=0 AND (title_input_hash IS NULL OR title_input_hash!=?)",
            (title, source, finished, job["input_hash"], finished, job["conversation_id"], job["input_hash"]),
        )
        db.execute(
            "UPDATE conversation_title_jobs SET status='completed',last_error=?,updated_at=?,finished_at=? WHERE id=?",
            (error, finished, finished, job_id),
        )
    result = _job(job_id) or {}
    result["title"] = title if updated.rowcount else None
    result["title_source"] = source
    return result


def schedule_title_generation(
    conversation_id: int,
    user_message: str,
    assistant_response: str,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    job = ensure_title_job(conversation_id, user_message, assistant_response)
    if not job or job["status"] == "completed":
        return job
    if _budgeted_conversation(conversation_id):
        return _local_title_job(job)
    task = asyncio.create_task(run_title_job(str(job["id"]), api_key), name=f"conversation-title-{conversation_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return job


async def start_title_runtime() -> None:
    with connect() as db:
        db.execute("UPDATE conversation_title_jobs SET status='pending',updated_at=? WHERE status='running'", (now_iso(),))
    for job in rows("SELECT id FROM conversation_title_jobs WHERE status IN ('pending','failed') ORDER BY created_at"):
        task = asyncio.create_task(run_title_job(str(job["id"])), name=f"conversation-title-recovery-{job['id']}")
        _running.add(task)
        task.add_done_callback(_running.discard)


async def stop_title_runtime() -> None:
    tasks = tuple(_running)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
