from __future__ import annotations

from typing import Any

from .database import connect, now_iso, rows
from .provider import completion


COMPACT_AFTER_CHARS = 48_000
KEEP_RECENT_MESSAGES = 10


def context_stats(conversation_id: int) -> dict[str, Any]:
    messages = rows("SELECT id, content FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))
    state = rows("SELECT * FROM conversation_context WHERE conversation_id=?", (conversation_id,))
    characters = sum(len(item["content"]) for item in messages)
    return {
        "message_count": len(messages),
        "estimated_tokens": characters // 3,
        "compacted_through": state[0]["compacted_through"] if state else 0,
        "has_summary": bool(state and state[0]["summary"]),
        "summary": state[0]["summary"] if state else "",
    }


async def compact_conversation(conversation_id: int, api_key: str | None, force: bool = False) -> dict[str, Any]:
    all_messages = rows("SELECT id, role, content FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,))
    state = rows("SELECT * FROM conversation_context WHERE conversation_id=?", (conversation_id,))
    through = state[0]["compacted_through"] if state else 0
    candidates = [item for item in all_messages[:-KEEP_RECENT_MESSAGES] if item["id"] > through]
    chars = sum(len(item["content"]) for item in all_messages)
    if not candidates or (not force and chars < COMPACT_AFTER_CHARS):
        return {"compacted": False, **context_stats(conversation_id)}
    previous = state[0]["summary"] if state else ""
    transcript = "\n".join(f"{item['role']}: {item['content']}" for item in candidates)
    prompt = [
        {"role": "system", "content": "压缩 Agent 会话。保留用户目标、已做决定、文件路径、代码改动、错误、待办和验证结果。不要加入新事实。输出简洁中文摘要。"},
        {"role": "user", "content": f"已有摘要：\n{previous or '无'}\n\n新增会话：\n{transcript}"},
    ]
    summary = (await completion(prompt, api_key, tools=[], conversation_id=conversation_id)).get("content") or previous
    last_id = candidates[-1]["id"]
    with connect() as db:
        db.execute(
            "INSERT INTO conversation_context(conversation_id, summary, compacted_through, updated_at) VALUES(?,?,?,?) ON CONFLICT(conversation_id) DO UPDATE SET summary=excluded.summary, compacted_through=excluded.compacted_through, updated_at=excluded.updated_at",
            (conversation_id, summary, last_id, now_iso()),
        )
    return {"compacted": True, **context_stats(conversation_id)}


def model_history(conversation_id: int) -> list[dict[str, str]]:
    state = rows("SELECT * FROM conversation_context WHERE conversation_id=?", (conversation_id,))
    through = state[0]["compacted_through"] if state else 0
    history: list[dict[str, str]] = []
    if state and state[0]["summary"]:
        history.append({"role": "system", "content": "此前会话摘要：\n" + state[0]["summary"]})
    history.extend({"role": item["role"], "content": item["content"]} for item in rows("SELECT role, content FROM messages WHERE conversation_id=? AND id>? ORDER BY id", (conversation_id, through)) if item["role"] in {"user", "assistant"})
    return history
