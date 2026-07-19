import asyncio
import uuid
from pathlib import Path

from app.database import connect, now_iso, rows
from app.routes.conversations import rename_conversation
from app.schemas import ChatRequest, ConversationRename
from app.task_runner import run_chat
from app.title_jobs import ensure_title_job, fallback_title, run_title_job, start_title_runtime, stop_title_runtime


def _conversation(tmp_path: Path, *, title: str = "新对话") -> tuple[int, str, str]:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    stamp = now_iso()
    user = "帮我修复 Tauri API 配置"
    assistant = "我会检查配置并给出验证结果。"
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, title, str(tmp_path), "ask", stamp, stamp),
        )
        db.execute(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
            (conversation_id, "user", user, stamp),
        )
        db.execute(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
            (conversation_id, "assistant", assistant, stamp),
        )
    return conversation_id, user, assistant


def test_title_job_generates_normalized_title_and_is_idempotent(tmp_path: Path) -> None:
    conversation_id, user, assistant = _conversation(tmp_path)
    job = ensure_title_job(conversation_id, user, assistant)
    duplicate = ensure_title_job(conversation_id, user, assistant)

    async def fake_completion(*_args, **_kwargs):
        return {"content": "“关于 Tauri API配置修复 的对话”"}

    result = asyncio.run(run_title_job(str(job["id"]), completion_fn=fake_completion))
    conversation = rows("SELECT * FROM conversations WHERE id=?", (conversation_id,))[0]

    assert duplicate["id"] == job["id"]
    assert result["title_source"] == "auto"
    assert conversation["title"] == "TauriAPI配置修复"
    assert conversation["title_source"] == "auto"
    assert conversation["title_version"] == 1
    assert conversation["title_input_hash"] == job["input_hash"]


def test_manual_title_lock_wins_over_running_job(tmp_path: Path) -> None:
    conversation_id, user, assistant = _conversation(tmp_path)
    job = ensure_title_job(conversation_id, user, assistant)
    renamed = rename_conversation(conversation_id, ConversationRename(title="我的固定标题"))

    async def fake_completion(*_args, **_kwargs):
        return {"content": "自动生成标题"}

    result = asyncio.run(run_title_job(str(job["id"]), completion_fn=fake_completion))
    conversation = rows("SELECT title,title_source,title_locked FROM conversations WHERE id=?", (conversation_id,))[0]

    assert renamed["title_locked"] is True
    assert result["title"] is None
    assert conversation == {"title": "我的固定标题", "title_source": "manual", "title_locked": 1}


def test_title_failure_uses_sensitive_safe_fallback(tmp_path: Path) -> None:
    conversation_id, _user, assistant = _conversation(tmp_path)
    private_path = "C:" + "\\Users\\someone\\private.txt"
    fake_secret = "s" + "k-1234567890abcdefghijkl"
    user = f"请处理 {private_path}，密钥 {fake_secret}"
    with connect() as db:
        db.execute("UPDATE messages SET content=? WHERE conversation_id=? AND role='user'", (user, conversation_id))
    job = ensure_title_job(conversation_id, user, assistant)

    async def failing_completion(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    result = asyncio.run(run_title_job(str(job["id"]), completion_fn=failing_completion))
    title = rows("SELECT title FROM conversations WHERE id=?", (conversation_id,))[0]["title"]

    assert result["title_source"] == "fallback"
    assert "sk-" not in title
    assert "Users" not in title
    assert "private.txt" not in title
    assert 4 <= len(title) <= 16


def test_title_runtime_recovers_running_job(tmp_path: Path, monkeypatch) -> None:
    conversation_id, user, assistant = _conversation(tmp_path)
    job = ensure_title_job(conversation_id, user, assistant)
    with connect() as db:
        db.execute("UPDATE conversation_title_jobs SET status='running' WHERE id=?", (job["id"],))

    async def fake_completion(*_args, **_kwargs):
        return {"content": "Tauri配置恢复"}

    monkeypatch.setattr("app.title_jobs.completion", fake_completion)

    async def exercise() -> None:
        await start_title_runtime()
        for _ in range(50):
            if rows("SELECT status FROM conversation_title_jobs WHERE id=?", (job["id"],))[0]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        await stop_title_runtime()

    asyncio.run(exercise())
    assert rows("SELECT status FROM conversation_title_jobs WHERE id=?", (job["id"],))[0]["status"] == "completed"


def test_fallback_title_obeys_english_word_limit() -> None:
    title = fallback_title("Please investigate the failing deployment pipeline and repair the release checks today")
    assert 3 <= len(title.split()) <= 8


def test_first_successful_response_schedules_title_without_waiting(tmp_path: Path, monkeypatch) -> None:
    conversation_id = uuid.uuid4().int % 1_000_000_000
    stamp = now_iso()
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (conversation_id, "新对话", "", "ask", stamp, stamp),
        )
    scheduled: list[tuple[int, str, str]] = []

    async def fake_completion(*_args, **_kwargs):
        return {"content": "这是首轮正常回答。"}

    monkeypatch.setattr(
        "app.task_runner.schedule_title_generation",
        lambda cid, user, answer, _api_key=None: scheduled.append((cid, user, answer)),
    )
    result = asyncio.run(run_chat(
        ChatRequest(conversation_id=conversation_id, content="总结今天的工作", task_id=uuid.uuid4().hex),
        completion_fn=fake_completion,
    ))

    assert result["task_status"] == "completed"
    assert scheduled == [(conversation_id, "总结今天的工作", "这是首轮正常回答。")]
